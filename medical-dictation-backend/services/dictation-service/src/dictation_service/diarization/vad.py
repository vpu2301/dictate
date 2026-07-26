"""Silero VAD segmentation tuned for diarization (sprint 14).

Distinct from ``asr_worker.vad`` on purpose: that wrapper merges segments
< 500 ms apart and caps them at 30 s for Whisper's context window — both
transformations destroy speaker-turn boundaries. Here we keep turns as
Silero reports them (250 ms min silence splits a doctor→patient hand-off
into two segments) and never merge across a potential turn change.

Fail-loud policy: diarization with a stubbed VAD would silently produce
garbage speaker labels on real consultations, so unlike the asr-worker
wrapper there is NO stub fallback — a missing silero-vad install raises
at load time and conversation sessions are refused (dictation mode is
unaffected; it never loads this module).
"""

from __future__ import annotations

from typing import Any

import numpy as np

SAMPLE_RATE_HZ = 16_000


class SileroSegmenter:
    """Speech-region detection over a PCM window. Thread-safe for the
    single-consumer pattern used by the diarization stream (one call at
    a time per process; the model itself is stateless between calls
    because we reset internal state per invocation)."""

    def __init__(
        self,
        *,
        min_speech_duration_ms: int = 150,
        min_silence_duration_ms: int = 150,
    ) -> None:
        self._min_speech_ms = min_speech_duration_ms
        self._min_silence_ms = min_silence_duration_ms
        self._model: Any = None
        self._get_speech_timestamps: Any = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        import torch
        from silero_vad import get_speech_timestamps, load_silero_vad

        torch.set_grad_enabled(False)
        self._model = load_silero_vad()
        self._get_speech_timestamps = get_speech_timestamps

    def speech_regions(self, pcm: np.ndarray) -> list[tuple[int, int]]:
        """Return [(start_ms, end_ms), ...] of speech inside ``pcm``
        (float32 mono 16 kHz), relative to the start of the buffer."""
        self._ensure_loaded()
        import torch

        if pcm.size == 0:
            return []
        tensor = torch.from_numpy(np.ascontiguousarray(pcm, dtype=np.float32))
        stamps = self._get_speech_timestamps(
            tensor,
            self._model,
            sampling_rate=SAMPLE_RATE_HZ,
            min_speech_duration_ms=self._min_speech_ms,
            min_silence_duration_ms=self._min_silence_ms,
        )
        return [
            (
                int(s["start"] * 1000 / SAMPLE_RATE_HZ),
                int(s["end"] * 1000 / SAMPLE_RATE_HZ),
            )
            for s in stamps
        ]
