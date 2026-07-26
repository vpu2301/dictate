"""Per-process diarization engine (sprint 14, ADR-0034).

One shared ECAPA embedder + Silero segmenter pair per process (the
models are stateless between calls; ~90 MB resident once), handing out
per-session :class:`DiarizationStream` instances that own the mutable
clustering state.

Loading is lazy + locked: dictation-only deployments (and macOS dev
without the model dir) never pay for torch imports or weights. A
conversation ``start_session`` triggers ``ensure_loaded()``; failure
raises :class:`DiarizationUnavailableError` and the session is refused —
fail-loud, never a silent stub producing garbage labels.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

from .embedder import EcapaEmbedder
from .stream import DiarizationConfig, DiarizationStream
from .vad import SileroSegmenter

logger = logging.getLogger(__name__)


class DiarizationUnavailableError(Exception):
    """Conversation mode requested but the diarizer cannot load."""


class DiarizationEngine:
    def __init__(self, *, model_dir: str, device: str = "cpu", enabled: bool = True) -> None:
        self._model_dir = model_dir
        self._device = device
        self._enabled = enabled
        self._embedder: EcapaEmbedder | None = None
        self._segmenter: SileroSegmenter | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def loaded(self) -> bool:
        return self._embedder is not None

    async def ensure_loaded(self) -> None:
        if not self._enabled:
            raise DiarizationUnavailableError("conversation mode disabled (MDX_CONVERSATION_ENABLED)")
        if self.loaded:
            return
        async with self._lock:
            if self.loaded:
                return
            embedder = EcapaEmbedder(model_dir=self._model_dir, device=self._device)
            segmenter = SileroSegmenter()
            try:
                # Weight loading + first forward are CPU/GPU-bound; keep
                # the event loop responsive for live dictation sessions.
                await asyncio.to_thread(embedder.warm_up)
                await asyncio.to_thread(
                    segmenter.speech_regions, np.zeros(1600, dtype=np.float32)
                )
            except Exception as exc:  # torch/model errors are varied; fail loud, typed
                logger.error(
                    "diarization.load_failed",
                    extra={"model_dir": self._model_dir, "error_class": type(exc).__name__},
                )
                raise DiarizationUnavailableError(
                    f"diarizer failed to load from {self._model_dir}: {type(exc).__name__}"
                ) from exc
            self._embedder = embedder
            self._segmenter = segmenter
            logger.info(
                "diarization.loaded",
                extra={"model_dir": self._model_dir, "device": self._device},
            )

    def new_stream(self, config: DiarizationConfig | None = None) -> DiarizationStream:
        if self._embedder is None or self._segmenter is None:
            raise DiarizationUnavailableError("diarizer not loaded; call ensure_loaded() first")
        return DiarizationStream(
            embedder=self._embedder,
            segmenter=self._segmenter,
            config=config,
        )
