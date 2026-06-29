"""Sprint-07 — the standing WER release gate's measurement run.

Two modes:

* ``--corpus eval/corpus`` (sprint-07): the manifest-backed reference
  corpus. Verifies SHA-256 integrity, then for each utterance runs the
  real Whisper code path, computes WER (UK-aware) + CER + RTF +
  per-category number-norm, writes a JSON report + a Markdown history
  entry + a Prometheus textfile, and (with ``--dsn``) persists
  ``audit.eval_runs`` + ``audit.eval_utterances`` so
  ``compare_to_baseline.py`` can gate on it.

* ``--fixtures tests/fixtures/wer`` (legacy sprint-03 harness): the
  ``WER_TARGETS`` smoke check used by ``make wer-eval``. Kept for
  backwards compatibility; ``--fail-on-regression`` exits non-zero if a
  ``(language, specialty)`` misses its pilot target.

Determinism (methodology §Determinism): every run records the model,
NLP ``pipeline_version``, prompts-corpus SHA-256, and corpus_version so
a regression is bisectable. Same ``(audio, model, prompts_hash,
pipeline_version)`` → identical scores.

Usage::

    python scripts/eval/run_wer.py --corpus eval/corpus \\
        --output eval/reports \\
        --metrics-file /var/lib/node_exporter/textfile_collector/mdx_wer.prom \\
        --dsn "$EVAL_DB_DSN"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_kinds  # noqa: E402
import wer_lib  # noqa: E402

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_CORPUS = REPO_ROOT / "infra" / "postgres" / "seed" / "medical_prompts.sql"

# ── Legacy reference targets (sprint-03 pilot baselines) ──────────────
WER_TARGETS: dict[tuple[str, str], float] = {
    ("uk", "general"): 0.18,
    ("uk", "cardiology"): 0.14,
    ("en", "general"): 0.10,
    ("en", "cardiology"): 0.08,
}


# ── Determinism provenance ────────────────────────────────────────────
def prompts_hash() -> str:
    """SHA-256 of the prompts corpus seed (methodology §Determinism)."""
    if PROMPTS_CORPUS.exists():
        return wer_lib.sha256_file(PROMPTS_CORPUS)
    return "unknown"


def pipeline_version() -> str:
    try:
        from nlp_service import PIPELINE_VERSION

        return PIPELINE_VERSION
    except Exception:  # noqa: BLE001 — nlp-service may not be importable in CI
        return "unknown"


# ══════════════════════════════════════════════════════════════════════
# New corpus mode
# ══════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class UtteranceScore:
    utterance_id: str
    language: str
    specialty: str
    duration_s: float
    wer: float
    n_ref_words: int
    cer: float
    rtf: float
    number_norm_score: float | None
    number_norm_by_category: dict[str, float]
    reference: str
    hypothesis: str


@dataclass(slots=True)
class RunResult:
    run_id: str
    started_at: str
    finished_at: str
    corpus_version: str
    model: str
    pipeline_version: str
    prompts_hash: str
    utterances: list[UtteranceScore] = field(default_factory=list)

    # ── Aggregates ────────────────────────────────────────────────────
    def _word_weighted_wer(self, lang: str) -> float | None:
        rows = [u for u in self.utterances if u.language == lang]
        denom = sum(u.n_ref_words for u in rows)
        if denom == 0:
            return None
        return sum(u.wer * u.n_ref_words for u in rows) / denom

    def _char_weighted_cer(self, lang: str) -> float | None:
        rows = [u for u in self.utterances if u.language == lang]
        denom = sum(len(u.reference) for u in rows)
        if denom == 0:
            return None
        return sum(u.cer * len(u.reference) for u in rows) / denom

    def wer_overall(self, lang: str) -> float | None:
        return self._word_weighted_wer(lang)

    def cer_overall(self, lang: str) -> float | None:
        return self._char_weighted_cer(lang)

    def specialty_wer(self) -> dict[tuple[str, str], float]:
        """Per (language, specialty) WER. Single-word references are
        excluded from the per-specialty mean (methodology §Edge cases)."""
        groups: dict[tuple[str, str], list[UtteranceScore]] = {}
        for u in self.utterances:
            if u.n_ref_words <= 1:
                continue
            groups.setdefault((u.language, u.specialty), []).append(u)
        out: dict[tuple[str, str], float] = {}
        for key, rows in groups.items():
            denom = sum(r.n_ref_words for r in rows)
            out[key] = sum(r.wer * r.n_ref_words for r in rows) / denom
        return out

    def rtf_percentile(self, p: float) -> float:
        return wer_lib.percentile([u.rtf for u in self.utterances], p)

    def number_norm_by_category(self) -> dict[str, float]:
        """Aggregate per-category number-norm across all utterances."""
        totals: dict[str, list[float]] = {}
        for u in self.utterances:
            for cat, score in u.number_norm_by_category.items():
                totals.setdefault(cat, []).append(score)
        return {cat: sum(v) / len(v) for cat, v in totals.items()}


def _transcribe_corpus(corpus_dir: Path, manifest: dict) -> RunResult:
    """Run the real Whisper path over every manifest utterance."""
    import asyncio

    from asr_worker.audio_io import decode_to_pcm
    from asr_worker.inference import WhisperEngine

    engine = WhisperEngine()
    engine.load()

    started = datetime.now(UTC)
    run = RunResult(
        run_id=str(uuid.uuid4()),
        started_at=started.isoformat(),
        finished_at=started.isoformat(),
        corpus_version=manifest.get("corpus_version", corpus_dir.name),
        model=engine.model_name,
        pipeline_version=pipeline_version(),
        prompts_hash=prompts_hash(),
    )

    for entry in manifest["utterances"]:
        sub = corpus_dir / entry["path"]
        audio_path = sub / "audio.wav"
        reference = (sub / "transcript.txt").read_text("utf-8").strip()
        duration_s = float(entry["duration_s"])

        async def run_one(audio_path=audio_path, entry=entry) -> str:
            audio = audio_path.read_bytes()
            pcm = await decode_to_pcm(audio)
            out = await engine.transcribe(
                pcm,
                language=entry["language"],
                prompt=entry.get("prompt"),
            )
            return " ".join(seg.text for seg in out.segments).strip()

        t0 = time.monotonic()
        hypothesis = asyncio.run(run_one())
        wall = max(time.monotonic() - t0, 1e-6)

        wer_val, n_ref = wer_lib.wer(reference, hypothesis)
        cer_val, _ = wer_lib.cer(reference, hypothesis)
        run.utterances.append(
            UtteranceScore(
                utterance_id=entry["utterance_id"],
                language=entry["language"],
                specialty=entry["specialty"],
                duration_s=duration_s,
                wer=wer_val,
                n_ref_words=n_ref,
                cer=cer_val,
                rtf=duration_s / wall,
                number_norm_score=wer_lib.number_norm_overall(reference, hypothesis),
                number_norm_by_category=wer_lib.number_norm_by_category(
                    reference, hypothesis
                ),
                reference=reference,
                hypothesis=hypothesis,
            )
        )
        logger.info(
            "wer.utterance id=%s lang=%s wer=%.3f cer=%.3f rtf=%.2f",
            entry["utterance_id"],
            entry["language"],
            wer_val,
            cer_val,
            duration_s / wall,
        )

    run.finished_at = datetime.now(UTC).isoformat()
    return run


def _report_dict(run: RunResult) -> dict:
    return {
        "run_id": run.run_id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "corpus_version": run.corpus_version,
        "model": run.model,
        "pipeline_version": run.pipeline_version,
        "prompts_hash": run.prompts_hash,
        "utterances": [
            {
                "utterance_id": u.utterance_id,
                "language": u.language,
                "specialty": u.specialty,
                "duration_s": u.duration_s,
                "wer": round(u.wer, 4),
                "cer": round(u.cer, 4),
                "rtf": round(u.rtf, 3),
                "number_norm_score": (
                    round(u.number_norm_score, 4)
                    if u.number_norm_score is not None
                    else None
                ),
                "number_norm_by_category": {
                    k: round(v, 4) for k, v in u.number_norm_by_category.items()
                },
                "reference": u.reference,
                "hypothesis": u.hypothesis,
            }
            for u in run.utterances
        ],
        "aggregates": {
            "wer_overall_uk": run.wer_overall("uk"),
            "wer_overall_en": run.wer_overall("en"),
            "cer_overall_uk": run.cer_overall("uk"),
            "cer_overall_en": run.cer_overall("en"),
            "rtf_p50": round(run.rtf_percentile(50), 3),
            "rtf_p95": round(run.rtf_percentile(95), 3),
            "number_norm_by_category": {
                k: round(v, 4) for k, v in run.number_norm_by_category().items()
            },
        },
    }


def _write_json_report(run: RunResult, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{run.run_id}.json"
    path.write_text(
        json.dumps(_report_dict(run), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _append_markdown_history(run: RunResult) -> Path:
    date = run.started_at[:10]
    hist_dir = REPO_ROOT / "docs" / "eval" / "wer-history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    path = hist_dir / f"{date}.md"

    def fmt(x: float | None) -> str:
        return f"{x:.4f}" if x is not None else "n/a"

    lines = [
        f"## Run {run.run_id} — {run.started_at}",
        "",
        f"- corpus_version: `{run.corpus_version}` | model: `{run.model}` "
        f"| pipeline: `{run.pipeline_version}` | prompts_hash: `{run.prompts_hash[:12]}…`",
        f"- WER uk: **{fmt(run.wer_overall('uk'))}**  WER en: **{fmt(run.wer_overall('en'))}**",
        f"- CER uk: {fmt(run.cer_overall('uk'))}  CER en: {fmt(run.cer_overall('en'))}",
        f"- RTF p50: {run.rtf_percentile(50):.3f}  p95: {run.rtf_percentile(95):.3f}",
        f"- number-norm: {run.number_norm_by_category()}",
        "",
    ]
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def _emit_prom(run: RunResult, path: Path) -> None:
    lines: list[str] = [
        "# HELP mdx_wer_overall Word error rate per language (0.0 – 1.0)",
        "# TYPE mdx_wer_overall gauge",
    ]
    for lang in ("uk", "en"):
        v = run.wer_overall(lang)
        if v is not None:
            lines.append(f'mdx_wer_overall{{language="{lang}"}} {v:.4f}')
    lines += [
        "# HELP mdx_wer_specialty WER per language+specialty",
        "# TYPE mdx_wer_specialty gauge",
    ]
    for (lang, spec), v in sorted(run.specialty_wer().items()):
        lines.append(
            f'mdx_wer_specialty{{language="{lang}",specialty="{spec}"}} {v:.4f}'
        )
    lines += [
        "# HELP mdx_cer_overall Character error rate per language",
        "# TYPE mdx_cer_overall gauge",
    ]
    for lang in ("uk", "en"):
        v = run.cer_overall(lang)
        if v is not None:
            lines.append(f'mdx_cer_overall{{language="{lang}"}} {v:.4f}')
    lines += [
        "# HELP mdx_rtf_p95 Realtime factor, 95th percentile",
        "# TYPE mdx_rtf_p95 gauge",
        f"mdx_rtf_p95 {run.rtf_percentile(95):.3f}",
        "# HELP mdx_number_norm_accuracy Per-category number-norm accuracy",
        "# TYPE mdx_number_norm_accuracy gauge",
    ]
    for cat, v in sorted(run.number_norm_by_category().items()):
        lines.append(f'mdx_number_norm_accuracy{{category="{cat}"}} {v:.4f}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _persist(run: RunResult, dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO audit.eval_runs (id, started_at, finished_at, "
            "corpus_version, model, pipeline_version, prompts_hash, utterances, "
            "wer_overall_uk, wer_overall_en, cer_overall_uk, cer_overall_en, "
            "rtf_p50, rtf_p95) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
            uuid.UUID(run.run_id),
            datetime.fromisoformat(run.started_at),
            datetime.fromisoformat(run.finished_at),
            run.corpus_version,
            run.model,
            run.pipeline_version,
            run.prompts_hash,
            len(run.utterances),
            run.wer_overall("uk"),
            run.wer_overall("en"),
            run.cer_overall("uk"),
            run.cer_overall("en"),
            run.rtf_percentile(50),
            run.rtf_percentile(95),
        )
        await conn.executemany(
            "INSERT INTO audit.eval_utterances (run_id, utterance_id, language, "
            "specialty, duration_s, wer, cer, rtf, number_norm_score, reference, "
            "hypothesis) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
            [
                (
                    uuid.UUID(run.run_id),
                    u.utterance_id,
                    u.language,
                    u.specialty,
                    u.duration_s,
                    u.wer,
                    u.cer,
                    u.rtf,
                    u.number_norm_score,
                    u.reference,
                    u.hypothesis,
                )
                for u in run.utterances
            ],
        )
    finally:
        await conn.close()


def run_corpus_mode(args: argparse.Namespace) -> int:
    import asyncio

    corpus_root: Path = args.corpus
    manifest_paths = sorted(corpus_root.glob("**/manifest.json"))
    if not manifest_paths:
        print(f"error: no manifest.json under {corpus_root}", file=sys.stderr)
        return 2

    overall_rc = 0
    for manifest_path in manifest_paths:
        corpus_dir = manifest_path.parent
        manifest = json.loads(manifest_path.read_text("utf-8"))

        # Integrity gate (methodology §corpus integrity).
        problems = wer_lib.verify_manifest(corpus_dir, manifest)
        if problems:
            print("=== corpus integrity FAILED ===", file=sys.stderr)
            for prob in problems:
                print("  " + prob, file=sys.stderr)
            return 3
        if not manifest["utterances"]:
            logger.warning("corpus %s has no utterances — skipping", corpus_dir)
            continue

        logger.info(
            "%s corpus=%s utterances=%d",
            audit_kinds.RUN_STARTED,
            manifest.get("corpus_version", corpus_dir.name),
            len(manifest["utterances"]),
        )
        run = _transcribe_corpus(corpus_dir, manifest)

        report = _write_json_report(run, args.output)
        hist = _append_markdown_history(run)
        print(f"wrote {report}")
        print(f"appended {hist}")
        if args.metrics_file is not None:
            _emit_prom(run, args.metrics_file)
            print(f"emitted {args.metrics_file}")

        for lang in ("uk", "en"):
            print(
                f"  WER {lang}: {run.wer_overall(lang)}  "
                f"CER {lang}: {run.cer_overall(lang)}"
            )
        print(f"  RTF p50/p95: {run.rtf_percentile(50):.3f} / {run.rtf_percentile(95):.3f}")
        print(f"  number-norm: {run.number_norm_by_category()}")

        if args.dsn:
            asyncio.run(_persist(run, args.dsn))
            print(f"  persisted run {run.run_id} to eval_runs/eval_utterances")

        logger.info(
            "%s run_id=%s wer_uk=%s wer_en=%s",
            audit_kinds.RUN_COMPLETED,
            run.run_id,
            run.wer_overall("uk"),
            run.wer_overall("en"),
        )

    return overall_rc


# ══════════════════════════════════════════════════════════════════════
# Legacy fixtures mode (sprint-03 — make wer-eval)
# ══════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class LegacyResult:
    language: str
    specialty: str
    wer: float
    n_ref_words: int
    n_files: int

    def passes(self) -> bool:
        target = WER_TARGETS.get((self.language, self.specialty))
        return target is None or self.wer <= target


def evaluate_fixtures(fixtures_dir: Path) -> list[LegacyResult]:
    import asyncio

    from asr_worker.audio_io import decode_to_pcm
    from asr_worker.inference import WhisperEngine

    engine = WhisperEngine()
    engine.load()

    grouped: dict[tuple[str, str], tuple[float, int, int]] = {}
    for manifest in fixtures_dir.glob("**/*.json"):
        doc = json.loads(manifest.read_text("utf-8"))
        audio_path = manifest.parent / doc["audio"]
        if not audio_path.exists():
            logger.warning("missing audio: %s", audio_path)
            continue

        async def run(audio_path=audio_path, doc=doc) -> str:
            audio = audio_path.read_bytes()
            pcm = await decode_to_pcm(audio)
            out = await engine.transcribe(
                pcm, language=doc["language"], prompt=doc.get("prompt")
            )
            return " ".join(seg.text for seg in out.segments).strip()

        hyp = asyncio.run(run())
        wer_val, n_ref = wer_lib.wer(doc["reference"], hyp)
        key = (doc["language"], doc["specialty"])
        prev_sum, prev_ref_words, prev_files = grouped.get(key, (0.0, 0, 0))
        grouped[key] = (prev_sum + wer_val * n_ref, prev_ref_words + n_ref, prev_files + 1)

    results: list[LegacyResult] = []
    for (lang, spec), (sum_, n_ref, n_files) in grouped.items():
        results.append(
            LegacyResult(
                language=lang,
                specialty=spec,
                wer=sum_ / n_ref if n_ref else 1.0,
                n_ref_words=n_ref,
                n_files=n_files,
            )
        )
    return results


def run_fixtures_mode(args: argparse.Namespace) -> int:
    results = evaluate_fixtures(args.fixtures)
    for r in results:
        target = WER_TARGETS.get((r.language, r.specialty))
        passed = "PASS" if r.passes() else "FAIL"
        print(
            f"{passed}  lang={r.language:2s}  spec={r.specialty:14s}  "
            f"WER={r.wer:.3f}  target={target if target else 'n/a'}  "
            f"n_files={r.n_files}  ref_words={r.n_ref_words}"
        )
    if args.metrics_file is not None:
        lines = ["# HELP mdx_asr_wer Word error rate (0.0 – 1.0)", "# TYPE mdx_asr_wer gauge"]
        for r in results:
            lines.append(
                f'mdx_asr_wer{{language="{r.language}",specialty="{r.specialty}"}} {r.wer:.4f}'
            )
        args.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        args.metrics_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.fail_on_regression and any(not r.passes() for r in results):
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus", type=Path, help="Manifest-backed corpus root (sprint-07 mode)."
    )
    parser.add_argument(
        "--fixtures", type=Path, help="Legacy per-JSON fixtures dir (sprint-03 mode)."
    )
    parser.add_argument("--output", type=Path, default=Path("eval/reports"))
    parser.add_argument("--metrics-file", type=Path, required=False)
    parser.add_argument("--dsn", required=False, help="Persist runs to audit.eval_runs.")
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Legacy mode: exit non-zero if a (language, specialty) misses its target.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.corpus and args.fixtures:
        parser.error("pass either --corpus or --fixtures, not both")
    if args.corpus:
        return run_corpus_mode(args)
    if args.fixtures:
        return run_fixtures_mode(args)
    parser.error("one of --corpus or --fixtures is required")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
