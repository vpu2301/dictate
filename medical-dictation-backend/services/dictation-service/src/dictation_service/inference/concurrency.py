"""Per-worker inference queue + concurrency cap.

A single asyncio queue serialises calls to ``WhisperEngine.transcribe_window``
across all live sessions on this process. Sessions submit windows; the
queue worker pulls them off and runs inference; results are awaited via
per-call futures. This prevents two windows from contending on the same
GPU at the same time (which would actually make both slower).

Cap of 4 sessions per worker comes from sprint-04 spec §9. When the
cap is reached, the WS upgrade handler rejects new sessions with
``gpu_full`` (recoverable: client retries after another session ends).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import numpy as np

logger = logging.getLogger(__name__)


class WorkerCapacityError(Exception):
    """Raised when the per-worker session cap is reached."""


@dataclass(slots=True)
class _Job:
    pcm: np.ndarray
    language: str
    prompt: str | None
    prev_text: str | None
    future: asyncio.Future[object]
    deadline_at: float


class InferenceQueue:
    """Serialise window-inference across sessions on this process.

    Construct once at startup; pass to each session's loop. Sessions call
    :meth:`submit` per window and ``await`` the returned future. The
    queue runs one background consumer that calls
    ``transcribe_window_fn`` per job.
    """

    def __init__(
        self,
        *,
        transcribe_window_fn: Callable[..., Awaitable[object]],
        deadline_multiplier: float,
        worker_id: str,
    ) -> None:
        self._fn = transcribe_window_fn
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._consumer_task: asyncio.Task[None] | None = None
        self._deadline_multiplier = deadline_multiplier
        self._worker_id = worker_id
        self._consecutive_deadline_misses = 0

    async def __aenter__(self) -> "InferenceQueue":
        self._consumer_task = asyncio.create_task(self._consume())
        return self

    async def __aexit__(self, *_: object) -> None:
        self._stop.set()
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except (asyncio.CancelledError, Exception):
                pass

    async def submit(
        self,
        pcm: np.ndarray,
        *,
        language: str,
        prompt: str | None,
        prev_text: str | None,
    ) -> object:
        """Enqueue a window for inference; return the WindowResult."""
        audio_seconds = pcm.shape[0] / 16_000.0
        deadline = time.monotonic() + max(
            2.0, audio_seconds * self._deadline_multiplier
        )
        fut: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        await self._queue.put(
            _Job(
                pcm=pcm,
                language=language,
                prompt=prompt,
                prev_text=prev_text,
                future=fut,
                deadline_at=deadline,
            )
        )
        return await fut

    @property
    def consecutive_deadline_misses(self) -> int:
        return self._consecutive_deadline_misses

    async def _consume(self) -> None:
        while not self._stop.is_set():
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            t0 = time.monotonic()
            try:
                result = await self._fn(
                    job.pcm,
                    language=job.language,
                    prompt=job.prompt,
                    prev_text=job.prev_text,
                )
                if t0 > job.deadline_at:
                    self._consecutive_deadline_misses += 1
                    logger.warning(
                        "inference.deadline_missed",
                        extra={
                            "worker_id": self._worker_id,
                            "consecutive": self._consecutive_deadline_misses,
                        },
                    )
                else:
                    self._consecutive_deadline_misses = 0
                if not job.future.done():
                    job.future.set_result(result)
            except Exception as exc:  # noqa: BLE001
                if not job.future.done():
                    job.future.set_exception(exc)
