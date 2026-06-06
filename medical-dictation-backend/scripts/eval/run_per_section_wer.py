"""Per-section WER eval — compare global vs section-specific prompt.

Sprint-06 DoD: ≥ 1 percentage-point WER improvement on UK cardiology
when using the section-specific ASR prompt vs sprint-03's global
cardiology prompt.

Fixtures at ``scripts/eval/fixtures/sprint-06-cardiology/`` are per-
utterance JSON manifests:

```json
{
  "audio": "anamnesis-001.wav",
  "language": "uk",
  "section": "anamnesis",       // matches cardiology_outpatient_uk section.id
  "reference": "<gold transcript>"
}
```

The script:
1. Loads ``infra/seeds/templates/cardiology_outpatient_uk.json`` to get
   per-section prompts.
2. For each manifest:
   a. Transcribes with the **global** prompt (sprint-03's
      cardiology prompt).
   b. Transcribes with the **section-specific** prompt.
3. Computes WER per utterance + aggregated by section.
4. Emits Prometheus gauges ``mdx_asr_wer_with_global_prompt`` and
   ``mdx_asr_wer_with_section_prompt``.
5. Documents results to ``docs/eval/sprint-06-section-prompt-wer.md``.

Run::

    python scripts/eval/run_per_section_wer.py \
        --fixtures scripts/eval/fixtures/sprint-06-cardiology \
        --template infra/seeds/templates/cardiology_outpatient_uk.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Sprint-03's global cardiology prompt (Ukrainian).
GLOBAL_CARDIOLOGY_PROMPT_UK = (
    "Кардіологічна консультація. Скарги, анамнез захворювання, "
    "фактори ризику, артеріальний тиск, ЧСС, аускультація серця, ЕКГ, "
    "ехокардіографія, тропоніни, NT-proBNP. Діагноз за класифікацією NYHA. "
    "Призначення: бета-блокатори, інгібітори АПФ, статини, антиагреганти."
)


@dataclass(slots=True)
class SectionWer:
    section: str
    n_utterances: int
    global_wer: float
    section_wer: float
    delta_pp: float


def _normalize(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"[^\w\s']+", " ", text, flags=re.UNICODE)
    return text.split()


def _wer(ref: str, hyp: str) -> float:
    ref_words = _normalize(ref)
    hyp_words = _normalize(hyp)
    n, m = len(ref_words), len(hyp_words)
    if n == 0:
        return 0.0 if m == 0 else 1.0
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        curr = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if ref_words[i - 1] == hyp_words[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[m] / n


def _load_template(path: Path) -> dict[str, str]:
    """Return {section_id: asr_prompt} from a template JSON file."""
    doc = json.loads(path.read_text("utf-8"))
    return {s["id"]: s["asr_prompt"] for s in doc.get("sections", [])}


async def _transcribe(audio_path: Path, language: str, prompt: str) -> str:
    """Run Whisper with the supplied prompt; returns hypothesis text.

    Imports faster-whisper lazily; CI environments without GPU + heavy
    models can run with ``--mock`` for plumbing tests.
    """
    from asr_worker.audio_io import decode_to_pcm
    from asr_worker.inference import WhisperEngine

    engine = WhisperEngine()
    engine.load()
    audio_bytes = audio_path.read_bytes()
    pcm = await decode_to_pcm(audio_bytes)
    out = await engine.transcribe(pcm, language=language, prompt=prompt)
    return " ".join(seg.text for seg in out.segments).strip()


def evaluate(fixtures_dir: Path, template_path: Path) -> list[SectionWer]:
    section_prompts = _load_template(template_path)
    grouped: dict[str, dict[str, list[float]]] = {}

    for manifest in sorted(fixtures_dir.glob("**/*.json")):
        doc = json.loads(manifest.read_text("utf-8"))
        audio_path = manifest.parent / doc["audio"]
        if not audio_path.exists():
            logger.warning("missing audio: %s", audio_path)
            continue
        section = doc["section"]
        if section not in section_prompts:
            logger.warning("section %s not in template", section)
            continue

        gold = doc["reference"]
        hyp_global = asyncio.run(
            _transcribe(audio_path, doc["language"], GLOBAL_CARDIOLOGY_PROMPT_UK)
        )
        hyp_section = asyncio.run(
            _transcribe(audio_path, doc["language"], section_prompts[section])
        )
        wer_g = _wer(gold, hyp_global)
        wer_s = _wer(gold, hyp_section)

        grouped.setdefault(section, {"global": [], "section": []})
        grouped[section]["global"].append(wer_g)
        grouped[section]["section"].append(wer_s)
        print(
            f"  {audio_path.name}: section={section} "
            f"global={wer_g:.3f} section_specific={wer_s:.3f}"
        )

    results: list[SectionWer] = []
    for section, vals in grouped.items():
        g_avg = sum(vals["global"]) / len(vals["global"])
        s_avg = sum(vals["section"]) / len(vals["section"])
        results.append(
            SectionWer(
                section=section,
                n_utterances=len(vals["global"]),
                global_wer=g_avg,
                section_wer=s_avg,
                delta_pp=(g_avg - s_avg) * 100.0,
            )
        )
    return results


def emit_prom(results: list[SectionWer], path: Path, *, language: str) -> None:
    lines: list[str] = [
        "# HELP mdx_asr_wer_with_global_prompt WER using sprint-03 global prompt",
        "# TYPE mdx_asr_wer_with_global_prompt gauge",
        "# HELP mdx_asr_wer_with_section_prompt WER using sprint-06 section prompt",
        "# TYPE mdx_asr_wer_with_section_prompt gauge",
    ]
    for r in results:
        lines.append(
            f'mdx_asr_wer_with_global_prompt{{language="{language}",section="{r.section}"}} {r.global_wer:.4f}'
        )
        lines.append(
            f'mdx_asr_wer_with_section_prompt{{language="{language}",section="{r.section}"}} {r.section_wer:.4f}'
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument(
        "--template",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "infra"
        / "seeds"
        / "templates"
        / "cardiology_outpatient_uk.json",
    )
    parser.add_argument("--language", default="uk")
    parser.add_argument("--metrics-file", type=Path)
    parser.add_argument(
        "--fail-on-no-improvement",
        action="store_true",
        help="Exit non-zero if any section's section-specific WER is "
        "≥ global WER (the spec's ≥1 pp improvement target).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.fixtures.exists():
        print(f"fixtures dir {args.fixtures} does not exist", file=sys.stderr)
        return 1

    results = evaluate(args.fixtures, args.template)
    print()
    print(f"{'section':<24}{'n':>4}{'global':>10}{'section':>10}{'Δ pp':>8}")
    print("-" * 56)
    for r in results:
        print(
            f"{r.section:<24}{r.n_utterances:>4}{r.global_wer:>10.3f}"
            f"{r.section_wer:>10.3f}{r.delta_pp:>+8.2f}"
        )

    if args.metrics_file is not None:
        args.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        emit_prom(results, args.metrics_file, language=args.language)

    if args.fail_on_no_improvement:
        # The DoD target: ≥ 1.0 pp improvement on at least one section
        # (cardiology aggregate) — if not hit, exit non-zero so CI fails.
        regressed = [r for r in results if r.delta_pp < 1.0]
        if regressed:
            print(
                f"\nWARN: {len(regressed)} section(s) below 1.0 pp improvement target",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
