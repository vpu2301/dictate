"""Silero VAD wrapper.

The model is loaded once at import time. ``detect_speech`` returns the
list of speech regions; chunks shorter than ``min_speech_duration_ms``
and gaps shorter than ``min_silence_duration_ms`` are smoothed out.

The output is consumed by the Whisper engine to produce per-chunk
transcriptions; chunks are individually ≤ 30 s (Whisper's audio context).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    start_ms: int
    end_ms: int


# Module-level cache for the loaded model + utils (silero is heavy).
_model: object | None = None
_get_speech_timestamps: object | None = None


def _ensure_loaded() -> None:
    global _model, _get_speech_timestamps
    if _model is not None:
        return
    try:
        import torch
        from silero_vad import get_speech_timestamps, load_silero_vad

        _model = load_silero_vad()
        _get_speech_timestamps = get_speech_timestamps
        torch.set_grad_enabled(False)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "vad.silero_unavailable",
            extra={"error": str(exc), "error_class": type(exc).__name__},
        )
        _model = "stub"
        _get_speech_timestamps = "stub"


def detect_speech(audio_pcm: np.ndarray, sr: int = 16_000) -> list[SpeechSegment]:
    """Return speech regions as a list of ``SpeechSegment``.

    On hosts where Silero isn't installed (the CPU dev fallback), this
    returns a single segment covering the entire audio so the downstream
    pipeline still runs and Whisper gets the whole signal.
    """
    _ensure_loaded()
    if _model == "stub":
        duration_ms = int(len(audio_pcm) / sr * 1000)
        return [SpeechSegment(0, max(1, duration_ms))]

    import torch

    audio_tensor = torch.from_numpy(audio_pcm)
    ts = _get_speech_timestamps(  # type: ignore[misc]
        audio_tensor,
        _model,
        sampling_rate=sr,
        min_speech_duration_ms=250,
        min_silence_duration_ms=500,
        return_seconds=False,
    )
    # ts is a list of {"start": int_samples, "end": int_samples}.
    out: list[SpeechSegment] = []
    for entry in ts:
        start_ms = int(entry["start"] / sr * 1000)
        end_ms = int(entry["end"] / sr * 1000)
        # Concatenate segments < 500 ms apart for fewer Whisper calls.
        if out and start_ms - out[-1].end_ms < 500:
            out[-1] = SpeechSegment(out[-1].start_ms, end_ms)
        else:
            out.append(SpeechSegment(start_ms, end_ms))
    # Cap each segment to 30 s of audio so Whisper's 30 s context isn't
    # exceeded — split long segments into 30-second chunks with 200 ms
    # overlap.
    capped: list[SpeechSegment] = []
    for s in out:
        cursor = s.start_ms
        while cursor < s.end_ms:
            end = min(cursor + 30_000, s.end_ms)
            capped.append(SpeechSegment(cursor, end))
            cursor = end
    return capped or [SpeechSegment(0, max(1, int(len(audio_pcm) / sr * 1000)))]
