"""End-to-end conversation-mode transcript proof (sprint 14).

Drives a fixture consultation through the REAL production streaming
path — ``StreamingWindower`` + ``WhisperEngine`` (ADR-0013 cadence) and
``DiarizationStream`` on the same windows — then applies the same
word-level attribution merge the WS handler uses, and prints the
diarized transcript with speaker turns.

This is the "a real two-voice consultation streams to a live transcript
with speaker turns" check: unlike run_der.py (which scores diarization
against ground truth), this one proves TEXT and SPEAKERS come out
together from the production components, and measures the partial
latency budget with both models resident (ADR-0013 §9: partial p95
≤ 1100 ms).

macOS/CPU auto-fallback mirrors scripts/eval/run_wer.py: tiny/int8/cpu
so the harness runs on a dev laptop. Whisper `tiny` transcribes
Ukrainian poorly — the ASR text here is NOT a quality signal (that is
the WER gate's job on the A10G rig); what this proves is the plumbing,
the turn structure, and the latency shape.

Usage:
    uv run python scripts/eval/run_conversation_e2e.py [--dialogue uk-consult-001]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services" / "dictation-service" / "src"))
sys.path.insert(0, str(REPO_ROOT / "services" / "asr-worker" / "src"))

# Must be set before asr_worker.config is imported (see run_wer.py).
if platform.system() == "Darwin":
    os.environ.setdefault("MD_ASR_DEVICE", "cpu")
    os.environ.setdefault("MD_ASR_COMPUTE_TYPE", "int8")
    os.environ.setdefault("MD_ASR_MODEL", "tiny")

import numpy as np  # noqa: E402

SAMPLE_RATE = 16_000
DEFAULT_MODEL_DIR = Path.home() / ".cache" / "mdx-models" / "ecapa-voxceleb"
# ADR-0013 §9 target; the release gate re-measures on the A10G rig.
PARTIAL_P95_BUDGET_MS = 1100


def _load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


async def run(args: argparse.Namespace) -> int:

    from asr_worker.inference import WhisperEngine
    from dictation_service.config import settings
    from dictation_service.diarization import (
        DiarizationStream,
        EcapaEmbedder,
        SileroSegmenter,
    )
    from dictation_service.diarization.mapping import SpeakerMappingInference
    from dictation_service.inference import StreamingWindower

    d = args.corpus / args.dialogue
    reference = json.loads((d / "reference.json").read_text(encoding="utf-8"))
    audio = _load_wav(d / "audio.wav")
    total_ms = audio.shape[0] * 1000 // SAMPLE_RATE
    language = reference["language"]

    print(f"dialogue={args.dialogue} language={language} duration={total_ms / 1000:.1f}s")
    print("loading models (Whisper + ECAPA + Silero)…")
    engine = WhisperEngine()
    engine.load()
    embedder = EcapaEmbedder(model_dir=str(args.model_dir), device="cpu")
    embedder.warm_up()

    windower = StreamingWindower(base_prompt="", language=language)
    diarization = DiarizationStream(embedder=embedder, segmenter=SileroSegmenter())
    mapping = SpeakerMappingInference(language=language)
    mapping_events: list[tuple[int, dict[str, str], float]] = []

    finals: list[dict[str, object]] = []
    partial_latencies: list[int] = []
    diar_latencies: list[float] = []
    infer_latencies: list[float] = []

    # Simulate the live stream: audio arrives in real time, the window
    # loop ticks every window_tick_interval_ms (600 ms).
    buffer_ms = 0
    tick_ms = settings.window_tick_interval_ms
    while buffer_ms < total_ms:
        buffer_ms = min(total_ms, buffer_ms + tick_ms)
        slice_ = windower.next_slice(buffer_total_ms=buffer_ms)
        if slice_ is None:
            continue
        pcm = audio[slice_.start_ms * SAMPLE_RATE // 1000 : slice_.end_ms * SAMPLE_RATE // 1000]

        t0 = time.perf_counter()
        window_result = await engine.transcribe_window(
            pcm,
            language=language,
            prompt="",
            prev_text=windower.build_prompt_for_next_window(),
        )
        infer_seconds = time.perf_counter() - t0
        infer_latencies.append(infer_seconds * 1000)

        t1 = time.perf_counter()
        diarization.process_window(pcm, window_start_ms=slice_.start_ms)
        diar_latencies.append((time.perf_counter() - t1) * 1000)

        tick = windower.integrate(
            window_segments=getattr(window_result, "segments", []),
            window_no_speech_prob=getattr(window_result, "no_speech_prob", 1.0),
            window_start_ms=slice_.start_ms,
            window_end_ms=slice_.end_ms,
            infer_seconds=infer_seconds,
            pcm_for_vad=pcm,
        )

        if tick.new_partial is not None:
            partial_latencies.append(max(0, buffer_ms - tick.new_partial.end_ms))

        for seg in tick.new_finals:
            speaker, conf = diarization.attribute(int(seg.start_ms), int(seg.end_ms))
            finals.append(
                {
                    "text": seg.text,
                    "start_ms": seg.start_ms,
                    "end_ms": seg.end_ms,
                    "speaker": speaker,
                    "speaker_confidence": conf,
                }
            )
            # Word-level feed for the doctor/patient inference (same as
            # the handler's _feed_mapping_inference).
            mapping.observe_segments(diarization.segments)
            for w in seg.words or []:
                w_speaker, _ = diarization.attribute(int(w.start_ms), int(w.end_ms))
                mapping.observe_word(w.text, w_speaker)
            hypothesis = mapping.evaluate()
            if hypothesis is not None:
                mapping_events.append(
                    (int(seg.end_ms), dict(hypothesis.mapping), hypothesis.confidence)
                )

    current = mapping.current
    roles = dict(current.mapping) if current else {}

    print("\n── diarized transcript (speaker turns) " + "─" * 30)
    labels = {"doctor": "ЛІКАР", "patient": "ПАЦІЄНТ"} if language == "uk" else {}
    for f in finals:
        spk = f["speaker"]
        role = roles.get(spk) if isinstance(spk, str) else None
        label = labels.get(role, role.upper() if role else (spk or "…"))
        conf = f["speaker_confidence"]
        conf_s = f"{conf:.2f}" if isinstance(conf, float) else "—"
        print(f"  [{f['start_ms']:>6}-{f['end_ms']:>6}ms] {label:<9} (conf {conf_s})  {f['text']}")

    print("\n── speaker mapping inference " + "─" * 38)
    if not mapping_events:
        print("  (no hypothesis reached the emit threshold)")
    for at_ms, m, conf in mapping_events:
        print(f"  @{at_ms:>6}ms  {m}  confidence={conf:.2f}")
    truth = {t["speaker"]: t["role"] for t in reference["turns"]}
    print(f"  fixture roles (ground truth): {truth}")

    labeled = sum(1 for f in finals if f["speaker"] in ("S1", "S2"))
    unknown = sum(1 for f in finals if f["speaker"] == "UNKNOWN")
    pending = sum(1 for f in finals if f["speaker"] is None)
    p95 = (
        sorted(partial_latencies)[int(0.95 * (len(partial_latencies) - 1))]
        if partial_latencies
        else 0
    )
    print("\n── metrics " + "─" * 56)
    print(f"  final segments: {len(finals)}  labeled={labeled} unknown={unknown} pending={pending}")
    print(f"  partial latency p95 = {p95} ms (budget {PARTIAL_P95_BUDGET_MS} ms)")
    print(
        f"  whisper/window p95 = {sorted(infer_latencies)[int(0.95 * (len(infer_latencies) - 1))]:.0f} ms"
        f" | diarization/window p95 = {sorted(diar_latencies)[int(0.95 * (len(diar_latencies) - 1))]:.0f} ms"
        f" ({100 * sum(diar_latencies) / max(1e-9, sum(infer_latencies) + sum(diar_latencies)):.0f}% of pipeline)"
    )
    ok = p95 <= PARTIAL_P95_BUDGET_MS and labeled > 0
    print(f"\n  RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialogue", default="uk-consult-001")
    parser.add_argument("--corpus", type=Path, default=REPO_ROOT / "eval" / "conversations" / "v1")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
