"""Run WER evaluation against the reference set in ``tests/fixtures/wer/``.

For each ``.json`` manifest in the fixtures dir, run the local
asr-worker (via the same WhisperEngine code path) against the WAV/MP3
file, then compute WER against the gold transcript. Emit Prometheus
textfile metrics that the node_exporter scrape picks up.

Usage::

    python scripts/eval/run_wer.py \\
        --fixtures tests/fixtures/wer \\
        --metrics-file /var/lib/node_exporter/wer.prom

CI runs this nightly via cron; manual invocations are common during
ML/MLOps tuning.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Reference targets per spec (pilot baselines) ──────────────────────
WER_TARGETS: dict[tuple[str, str], float] = {
    ("uk", "general"): 0.18,
    ("uk", "cardiology"): 0.14,
    ("en", "general"): 0.10,
    ("en", "cardiology"): 0.08,
}


@dataclass(slots=True)
class WerResult:
    language: str
    specialty: str
    wer: float
    n_ref_words: int
    n_files: int

    def passes(self) -> bool:
        target = WER_TARGETS.get((self.language, self.specialty))
        return target is None or self.wer <= target


def _normalize(text: str) -> list[str]:
    """Lowercase, strip punctuation, split on whitespace."""
    import re

    text = text.lower()
    text = re.sub(r"[^\w\s']+", " ", text, flags=re.UNICODE)
    return text.split()


def _wer(ref: str, hyp: str) -> tuple[float, int]:
    """Levenshtein-based WER. Returns (WER, ref-word-count)."""
    ref_words = _normalize(ref)
    hyp_words = _normalize(hyp)
    n = len(ref_words)
    m = len(hyp_words)
    if n == 0:
        return 0.0 if m == 0 else 1.0, 0

    prev = list(range(m + 1))
    for i in range(1, n + 1):
        curr = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if ref_words[i - 1] == hyp_words[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,  # deletion
                curr[j - 1] + 1,  # insertion
                prev[j - 1] + cost,  # substitution
            )
        prev = curr
    return prev[m] / n, n


def evaluate(fixtures_dir: Path) -> list[WerResult]:
    """Walk ``fixtures_dir`` and compute WER per (language, specialty)."""
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
                pcm,
                language=doc["language"],
                prompt=doc.get("prompt"),
            )
            return " ".join(seg.text for seg in out.segments).strip()

        hyp = asyncio.run(run())
        ref = doc["reference"]
        wer, n_ref = _wer(ref, hyp)
        logger.info(
            "wer.file",
            extra={
                "file": audio_path.name,
                "language": doc["language"],
                "specialty": doc["specialty"],
                "wer": round(wer, 3),
            },
        )

        key = (doc["language"], doc["specialty"])
        prev_sum, prev_ref_words, prev_files = grouped.get(key, (0.0, 0, 0))
        # Weighted by reference word count (more words = more weight).
        grouped[key] = (
            prev_sum + wer * n_ref,
            prev_ref_words + n_ref,
            prev_files + 1,
        )

    results: list[WerResult] = []
    for (lang, spec), (sum_, n_ref, n_files) in grouped.items():
        agg_wer = sum_ / n_ref if n_ref else 1.0
        results.append(
            WerResult(
                language=lang, specialty=spec, wer=agg_wer, n_ref_words=n_ref, n_files=n_files
            )
        )
    return results


def emit_prom(results: list[WerResult], path: Path) -> None:
    lines: list[str] = [
        "# HELP mdx_asr_wer Word error rate (0.0 – 1.0)",
        "# TYPE mdx_asr_wer gauge",
    ]
    for r in results:
        lines.append(
            f'mdx_asr_wer{{language="{r.language}",specialty="{r.specialty}"}} {r.wer:.4f}'
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--metrics-file", type=Path, required=False)
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit non-zero if any (language, specialty) misses its WER target.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    results = evaluate(args.fixtures)
    for r in results:
        target = WER_TARGETS.get((r.language, r.specialty))
        passed = "PASS" if r.passes() else "FAIL"
        print(
            f"{passed}  lang={r.language:2s}  spec={r.specialty:14s}  "
            f"WER={r.wer:.3f}  target={target if target else 'n/a'}  "
            f"n_files={r.n_files}  ref_words={r.n_ref_words}"
        )

    if args.metrics_file is not None:
        args.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        emit_prom(results, args.metrics_file)

    if args.fail_on_regression and any(not r.passes() for r in results):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
