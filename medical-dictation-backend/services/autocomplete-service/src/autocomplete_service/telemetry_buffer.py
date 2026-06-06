"""In-memory telemetry batch buffer.

Receives rows from the telemetry router; flushes every
``flush_interval_s`` OR when the buffer reaches ``flush_batch``,
whichever first. Sprint-10 service ships a single instance per
worker.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import asyncpg

from . import repository as repo

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable


class TelemetryBuffer:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        flush_interval_s: float,
        flush_batch: int,
    ) -> None:
        self._pool = pool
        self._interval = flush_interval_s
        self._batch = flush_batch
        self._rows: list[tuple] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    def append(self, row: tuple) -> None:
        self._rows.append(row)
        if len(self._rows) >= self._batch:
            asyncio.create_task(self._flush_locked(), name="telemetry-batch-flush")

    async def _flush_locked(self) -> None:
        async with self._lock:
            if not self._rows:
                return
            batch = self._rows
            self._rows = []
            try:
                async with self._pool.acquire() as conn:
                    await repo.insert_telemetry_batch(conn, batch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("telemetry.batch_insert_failed: %s", exc)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="telemetry-buffer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self._flush_locked()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._flush_locked()
