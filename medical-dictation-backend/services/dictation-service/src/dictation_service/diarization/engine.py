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
import time

import numpy as np

from .embedder import EcapaEmbedder
from .integrity import verify_model_dir
from .stream import DiarizationConfig, DiarizationStream
from .vad import SileroSegmenter

logger = logging.getLogger(__name__)


class DiarizationUnavailableError(Exception):
    """Conversation mode requested but the diarizer cannot load."""


class DiarizationEngine:
    def __init__(
        self,
        *,
        model_dir: str,
        device: str = "cpu",
        enabled: bool = True,
        pins: dict[str, str] | None = None,
        model_repo: str = "",
        model_revision: str = "",
    ) -> None:
        self._model_dir = model_dir
        self._device = device
        self._enabled = enabled
        self._pins = pins or {}
        self._model_repo = model_repo
        self._model_revision = model_revision
        self._embedder: EcapaEmbedder | None = None
        self._segmenter: SileroSegmenter | None = None
        self._lock = asyncio.Lock()
        # Set once a load attempt has failed, so readiness can report WHY a
        # worker is not advertising conversation capacity.
        self._last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def loaded(self) -> bool:
        return self._embedder is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def model_dir(self) -> str:
        return self._model_dir

    @property
    def device(self) -> str:
        return self._device

    @property
    def ready_for_conversation(self) -> bool:
        """True iff this worker can take a conversation session RIGHT NOW.

        Readiness gates on this: a worker advertising conversation capacity
        with a cold diarizer would pay weight-loading on the first window
        and blow the latency budget (sprint-14 deployment).
        """
        return self._enabled and self.loaded

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
                # Assert the BUILD-time digests again before the weights are
                # ever loaded (docs/models/PINS.md). Hashing 83 MB is I/O
                # bound — off-thread like the load itself.
                await asyncio.to_thread(
                    verify_model_dir,
                    self._model_dir,
                    pins=self._pins,
                    repo=self._model_repo,
                    revision=self._model_revision,
                )
                # Weight loading + first forward are CPU/GPU-bound; keep
                # the event loop responsive for live dictation sessions.
                await asyncio.to_thread(embedder.warm_up)
                await asyncio.to_thread(
                    segmenter.speech_regions, np.zeros(1600, dtype=np.float32)
                )
            except Exception as exc:  # torch/model errors are varied; fail loud, typed
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "diarization.load_failed",
                    extra={"model_dir": self._model_dir, "error_class": type(exc).__name__},
                )
                raise DiarizationUnavailableError(
                    f"diarizer failed to load from {self._model_dir}: {type(exc).__name__}"
                ) from exc
            self._embedder = embedder
            self._segmenter = segmenter
            self._last_error = None
            logger.info(
                "diarization.loaded",
                extra={"model_dir": self._model_dir, "device": self._device},
            )

    async def warm_up(self) -> bool:
        """Startup warmup: load both models, but never block service start.

        A dictation-only deployment (or a dev box with no model dir) must
        still serve dictation. The failure is recorded and surfaced by
        ``/readyz`` as *no conversation capacity*, and any conversation
        ``start_session`` is refused with a typed error — never a silent
        stub producing garbage labels.
        """
        if not self._enabled:
            logger.info("diarization.warmup_skipped", extra={"reason": "disabled"})
            return False
        t0 = time.monotonic()
        try:
            await self.ensure_loaded()
        except DiarizationUnavailableError as exc:
            logger.error(
                "diarization.warmup_failed",
                extra={
                    "model_dir": self._model_dir,
                    "error": str(exc),
                    "impact": "worker serves dictation only; conversation sessions refused",
                },
            )
            return False
        logger.info(
            "diarization.warmed",
            extra={
                "model_dir": self._model_dir,
                "device": self._device,
                "warmup_ms": round((time.monotonic() - t0) * 1000, 1),
            },
        )
        return True

    def new_stream(self, config: DiarizationConfig | None = None) -> DiarizationStream:
        if self._embedder is None or self._segmenter is None:
            raise DiarizationUnavailableError("diarizer not loaded; call ensure_loaded() first")
        return DiarizationStream(
            embedder=self._embedder,
            segmenter=self._segmenter,
            config=config,
        )
