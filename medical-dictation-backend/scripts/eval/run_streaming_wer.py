"""Streaming WER harness.

Replays a reference audio file end-to-end through the streaming
pipeline (encode → wire frames → decode → window → emit) and computes
WER against the gold transcript. Targets parity within 1 absolute
point of sprint-03's batch WER (sprint-04 spec §5).

Two modes:
  --simulated   in-process; no live WS. Drives the windower directly
                from PCM and reconstructs the would-be transcript.
                Fast, deterministic, used in CI.
  --live        opens a WS to dictation-service, streams Opus frames,
                consumes server-emitted messages. Slow, dev only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


WER_TARGETS: dict[tuple[str, str], float] = {
    ("uk", "general"): 0.19,
    ("uk", "cardiology"): 0.15,
    ("en", "general"): 0.11,
    ("en", "cardiology"): 0.09,
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
    import re

    text = text.lower()
    text = re.sub(r"[^\w\s']+", " ", text, flags=re.UNICODE)
    return text.split()


def _wer(ref: str, hyp: str) -> tuple[float, int]:
    ref_words = _normalize(ref)
    hyp_words = _normalize(hyp)
    n, m = len(ref_words), len(hyp_words)
    if n == 0:
        return 0.0 if m == 0 else 1.0, 0
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        curr = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if ref_words[i - 1] == hyp_words[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[m] / n, n


async def _simulate_one(audio_path: Path, language: str, prompt: str | None) -> str:
    """Drive the windower directly without a WS. Returns the final transcript."""
    from asr_worker.audio_io import decode_to_pcm
    from asr_worker.inference import WhisperEngine
    from dictation_service.inference.windower import StreamingWindower

    engine = WhisperEngine()
    engine.load()

    audio_bytes = audio_path.read_bytes()
    pcm = await decode_to_pcm(audio_bytes)
    windower = StreamingWindower(base_prompt=prompt or "", language=language)

    cursor_ms = 0
    sample_rate = 16_000
    window_ms = int(windower.window_s * 1000)
    overlap_ms = int(windower.overlap_s * 1000)
    step_ms = window_ms - overlap_ms

    while cursor_ms + window_ms <= pcm.shape[0] * 1000 // sample_rate:
        start_ms = cursor_ms
        end_ms = cursor_ms + window_ms
        chunk = pcm[start_ms * sample_rate // 1000: end_ms * sample_rate // 1000]
        result = await engine.transcribe_window(
            chunk,
            language=language,
            prompt=windower.base_prompt,
            prev_text=windower.build_prompt_for_next_window(),
        )
        windower.integrate(
            window_segments=result.segments,
            window_no_speech_prob=result.no_speech_prob,
            window_start_ms=start_ms,
            window_end_ms=end_ms,
            infer_seconds=result.infer_seconds,
            pcm_for_vad=chunk,
        )
        cursor_ms += step_ms

    return " ".join(w.text for w in windower.finalized_words).strip()


def evaluate(fixtures_dir: Path) -> list[WerResult]:
    grouped: dict[tuple[str, str], tuple[float, int, int]] = {}
    for manifest in fixtures_dir.glob("**/*.json"):
        doc = json.loads(manifest.read_text("utf-8"))
        audio_path = manifest.parent / doc["audio"]
        if not audio_path.exists():
            logger.warning("missing audio: %s", audio_path)
            continue
        hyp = asyncio.run(_simulate_one(audio_path, doc["language"], doc.get("prompt")))
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
        grouped[key] = (
            prev_sum + wer * n_ref,
            prev_ref_words + n_ref,
            prev_files + 1,
        )

    results: list[WerResult] = []
    for (lang, spec), (sum_, n_ref, n_files) in grouped.items():
        agg_wer = sum_ / n_ref if n_ref else 1.0
        results.append(
            WerResult(language=lang, specialty=spec, wer=agg_wer,
                      n_ref_words=n_ref, n_files=n_files)
        )
    return results


def emit_prom(results: list[WerResult], path: Path) -> None:
    lines: list[str] = [
        "# HELP mdx_dictation_streaming_wer Streaming WER (0.0 – 1.0)",
        "# TYPE mdx_dictation_streaming_wer gauge",
    ]
    for r in results:
        lines.append(
            f'mdx_dictation_streaming_wer{{language="{r.language}",specialty="{r.specialty}"}} {r.wer:.4f}'
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--metrics-file", type=Path)
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
