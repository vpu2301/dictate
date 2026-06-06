"""Whisper inference engine wrapper.

``faster-whisper`` is the chosen backend (ADR-0009). The engine is
stateless across calls; ``transcribe`` is safe to call sequentially.
This file deliberately knows nothing about queues, DBs, or storage —
it transforms PCM in, segments + metadata out.

Optional imports: ``faster_whisper`` is a heavy dependency, not
installable on macOS arm64 hosts without coercing the wheel. The module
imports it lazily so the asr-service (which only imports the output
types) doesn't pay the cost.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from asr_models import Segment, TranscriptionMetadata, TranscriptionOutput, WordTiming

from .config import settings
from .vad import SpeechSegment, detect_speech

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WindowResult:
    """One streaming-window inference outcome (sprint 04).

    Used by dictation-service's windower. Window-relative timestamps;
    the caller adds the window offset to land in session-absolute time.
    """

    segments: list[Segment]
    avg_logprob: float
    no_speech_prob: float
    infer_seconds: float


class WhisperEngine:
    """Lazily-loaded faster-whisper model.

    Instantiate once at process startup; reuse across jobs. The first
    ``transcribe`` after construction runs the warmup pass (5 s of
    silence) that JITs the CUDA kernels.
    """

    def __init__(self) -> None:
        self._model: Any | None = None
        self._loaded = False
        self._warm = False
        self._warmup_seconds: float = 0.0

    @property
    def model_name(self) -> str:
        return settings.asr_model

    @property
    def warmup_seconds(self) -> float:
        return self._warmup_seconds

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        """Eagerly load the model.

        Synchronous because faster-whisper's loader holds the GIL and
        kicks off CUDA kernel compilation that doesn't yield. Call
        once from the worker's startup path.
        """
        if self._loaded:
            return
        from faster_whisper import WhisperModel  # local import

        device = settings.asr_device
        compute_type = settings.asr_compute_type
        logger.info(
            "whisper.loading",
            extra={
                "model": settings.asr_model,
                "device": device,
                "compute_type": compute_type,
            },
        )
        t0 = time.monotonic()
        self._model = WhisperModel(
            settings.asr_model,
            device=device,
            compute_type=compute_type,
        )
        self._loaded = True
        # Synthetic warm-up on 5 s of silence: forces CUDA kernel JIT
        # and caches the audio frontend so the first real job's latency
        # isn't dominated by setup.
        silence = np.zeros(int(16_000 * 5), dtype=np.float32)
        try:
            _ = list(self._model.transcribe(silence, language="en", beam_size=1)[0])
        except Exception as exc:  # noqa: BLE001 — warm-up failure is non-fatal
            logger.warning(
                "whisper.warmup_failed",
                extra={"error": str(exc), "error_class": type(exc).__name__},
            )
        else:
            self._warm = True
        self._warmup_seconds = time.monotonic() - t0
        logger.info(
            "whisper.loaded",
            extra={"warmup_seconds": round(self._warmup_seconds, 2)},
        )

    async def transcribe(
        self,
        audio_pcm: np.ndarray,
        *,
        language: str,
        prompt: str | None,
        prompt_id: Any | None = None,
    ) -> TranscriptionOutput:
        """Run VAD + Whisper on the full audio.

        ``audio_pcm`` is mono 16 kHz float32 in [-1, 1].
        Offloads the (blocking) Whisper call to a thread so the asyncio
        loop stays responsive — important because the processor also
        polls the DB for cancellation between chunks.
        """
        if not self._loaded:
            raise RuntimeError(
                "WhisperEngine.load() must be called before transcribe()"
            )
        t_start = time.monotonic()
        speech = detect_speech(audio_pcm)
        vad_seconds_speech = sum((s.end_ms - s.start_ms) / 1000.0 for s in speech)

        loop = asyncio.get_running_loop()
        segments_out: list[Segment] = []
        for s in speech:
            chunk = audio_pcm[
                int(s.start_ms * 16): int(s.end_ms * 16)
            ]  # ms → samples (16 samples/ms @ 16kHz)
            if chunk.size == 0:
                continue
            segs = await loop.run_in_executor(
                None,
                self._run_chunk,
                chunk,
                language,
                prompt,
                s.start_ms,
            )
            segments_out.extend(segs)

        infer_seconds = time.monotonic() - t_start
        meta = TranscriptionMetadata(
            model=settings.asr_model,
            prompt_id=prompt_id,
            vad_seconds_speech=vad_seconds_speech,
            infer_seconds=infer_seconds,
            gpu_seconds=infer_seconds if settings.asr_device == "cuda" else 0.0,
            peak_gpu_mem_mb=_peak_gpu_mem_mb(),
            beam_size=settings.asr_beam_size,
        )
        return TranscriptionOutput(
            language=language, segments=segments_out, metadata=meta
        )

    async def transcribe_window(
        self,
        pcm: np.ndarray,
        *,
        language: str,
        prompt: str | None,
        prev_text: str | None = None,
    ) -> WindowResult:
        """Run inference on one streaming window (sprint 04 entry point).

        ``pcm`` is mono 16 kHz float32. Timestamps in the returned
        segments are window-relative (window-start = 0 ms); the caller
        adds the window's absolute offset.

        Unlike :meth:`transcribe`, this method is stateless: no VAD
        chunking, no aggregation. The dictation-service's windower owns
        the sliding-window state.
        """
        if not self._loaded:
            raise RuntimeError(
                "WhisperEngine.load() must be called before transcribe_window()"
            )
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        combined_prompt = _combine_prompts(prompt, prev_text)
        segs, avg_logprob, no_speech_prob = await loop.run_in_executor(
            None,
            self._run_window,
            pcm,
            language,
            combined_prompt,
        )
        return WindowResult(
            segments=segs,
            avg_logprob=avg_logprob,
            no_speech_prob=no_speech_prob,
            infer_seconds=time.monotonic() - t0,
        )

    def _run_window(
        self,
        pcm: np.ndarray,
        language: str,
        prompt: str | None,
    ) -> tuple[list[Segment], float, float]:
        assert self._model is not None
        result_segs, _info = self._model.transcribe(
            pcm,
            language=language,
            initial_prompt=prompt,
            word_timestamps=True,
            beam_size=settings.asr_beam_size,
            condition_on_previous_text=False,  # caller owns context via prompt
        )
        segments: list[Segment] = []
        logprobs: list[float] = []
        no_speech_probs: list[float] = []
        for seg in result_segs:
            words: list[WordTiming] = []
            if getattr(seg, "words", None):
                for w in seg.words:
                    words.append(
                        WordTiming(
                            text=w.word.strip(),
                            start_ms=int(w.start * 1000),
                            end_ms=int(w.end * 1000),
                            probability=float(getattr(w, "probability", 1.0)),
                        )
                    )
            avg_conf = (
                float(sum(w.probability for w in words)) / len(words)
                if words
                else 0.5
            )
            segments.append(
                Segment(
                    text=seg.text.strip(),
                    start_ms=int(seg.start * 1000),
                    end_ms=int(seg.end * 1000),
                    words=words,
                    avg_confidence=max(0.0, min(1.0, avg_conf)),
                )
            )
            logprobs.append(float(getattr(seg, "avg_logprob", -0.5)))
            no_speech_probs.append(float(getattr(seg, "no_speech_prob", 0.0)))
        avg_logprob = sum(logprobs) / len(logprobs) if logprobs else -1.0
        # Use the worst no_speech_prob across segments so a tail-of-silence
        # high-prob segment isn't averaged away.
        worst_no_speech = max(no_speech_probs) if no_speech_probs else 1.0
        return segments, avg_logprob, worst_no_speech

    def _run_chunk(
        self,
        chunk: np.ndarray,
        language: str,
        prompt: str | None,
        offset_ms: int,
    ) -> list[Segment]:
        assert self._model is not None
        result_segs, _info = self._model.transcribe(
            chunk,
            language=language,
            initial_prompt=prompt,
            word_timestamps=True,
            beam_size=settings.asr_beam_size,
            condition_on_previous_text=True,
        )
        out: list[Segment] = []
        for seg in result_segs:
            words: list[WordTiming] = []
            if getattr(seg, "words", None):
                for w in seg.words:
                    words.append(
                        WordTiming(
                            text=w.word.strip(),
                            start_ms=int(w.start * 1000) + offset_ms,
                            end_ms=int(w.end * 1000) + offset_ms,
                            probability=float(getattr(w, "probability", 1.0)),
                        )
                    )
            avg_conf = (
                float(sum(w.probability for w in words)) / len(words)
                if words
                else float(getattr(seg, "avg_logprob", -0.5) + 1.0) / 1.0
            )
            avg_conf = max(0.0, min(1.0, avg_conf))
            out.append(
                Segment(
                    text=seg.text.strip(),
                    start_ms=int(seg.start * 1000) + offset_ms,
                    end_ms=int(seg.end * 1000) + offset_ms,
                    words=words,
                    avg_confidence=avg_conf,
                )
            )
        return out


@dataclass(slots=True)
class _GpuInfo:
    peak_mb: int


def _peak_gpu_mem_mb() -> int:
    """Best-effort GPU peak memory in MB.

    Uses ``torch.cuda.max_memory_allocated`` if torch + CUDA are present;
    returns 0 otherwise (CPU fallback, or no torch in the image).
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return 0
        peak_bytes = torch.cuda.max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()
        return int(peak_bytes / (1024 * 1024))
    except Exception:
        return 0


def _combine_prompts(base: str | None, prev_text: str | None) -> str | None:
    """Join clinician-specialty prompt with the last-finalized text.

    Strips any Whisper special tokens (``<|...|>``) defensively. The
    caller is responsible for truncating prev_text to a token budget;
    here we just concatenate.
    """
    import re

    parts: list[str] = []
    if base:
        parts.append(re.sub(r"<\|[^|]+\|>", "", base).strip())
    if prev_text:
        parts.append(re.sub(r"<\|[^|]+\|>", "", prev_text).strip())
    text = " ".join(p for p in parts if p)
    return text or None


# Re-export so callers don't have to import vad themselves.
__all__ = ["WhisperEngine", "WindowResult", "SpeechSegment"]
