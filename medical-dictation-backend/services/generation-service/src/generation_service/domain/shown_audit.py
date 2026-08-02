"""Aggregated ``layer_c.completion.shown`` audit (sprint-08 buffer pattern).

One audit row per keystroke would pollute the hash chain (the
report-service ``DraftAuditBuffer`` precedent), so served completions
are counted per tenant in memory and flushed as ONE aggregated event
per tenant every flush interval. Losing a count on crash is acceptable;
the filtered events (warn) are NOT buffered — those write immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

logger = logging.getLogger(__name__)

FlushFn = Callable[[UUID, int], Awaitable[None]]


class ShownAuditBuffer:
    def __init__(self, *, flush_fn: FlushFn, flush_interval_s: float = 600.0) -> None:
        self._flush_fn = flush_fn
        self._flush_interval_s = flush_interval_s
        self._lock = asyncio.Lock()
        self._counts: dict[UUID, int] = {}
        self._task: asyncio.Task[None] | None = None

    async def record(self, *, tenant_id: UUID) -> None:
        async with self._lock:
            self._counts[tenant_id] = self._counts.get(tenant_id, 0) + 1

    async def flush_all(self) -> None:
        async with self._lock:
            snapshot = list(self._counts.items())
            self._counts.clear()
        for tenant_id, count in snapshot:
            try:
                await self._flush_fn(tenant_id, count)
            except Exception as exc:  # noqa: BLE001
                logger.warning("generation.shown_audit_flush_failed: %s", exc)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="shown-audit-buffer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await self._task
            self._task = None
        await self.flush_all()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval_s)
            await self.flush_all()
