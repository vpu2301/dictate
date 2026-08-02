"""Aggregated ``search.expanded`` audit (sprint 15, ADR-0038).

The DraftAuditBuffer volume pattern: expanded searches are counted per
tenant in memory and flushed as ONE audit row per tenant per interval.
Payload carries counts only — never the query text.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Entry:
    count: int = 0
    expanded_terms_total: int = 0


FlushFn = Callable[[UUID, int, int], Awaitable[None]]


class SearchAuditBuffer:
    def __init__(self, *, flush_fn: FlushFn, flush_interval_s: float = 600.0) -> None:
        self._flush_fn = flush_fn
        self._flush_interval_s = flush_interval_s
        self._lock = asyncio.Lock()
        self._buf: dict[UUID, _Entry] = {}
        self._task: asyncio.Task[None] | None = None

    async def record(self, *, tenant_id: UUID, expanded_terms: int) -> None:
        async with self._lock:
            entry = self._buf.setdefault(tenant_id, _Entry())
            entry.count += 1
            entry.expanded_terms_total += expanded_terms

    async def flush_all(self) -> None:
        async with self._lock:
            snapshot = list(self._buf.items())
            self._buf.clear()
        for tenant_id, entry in snapshot:
            try:
                await self._flush_fn(tenant_id, entry.count, entry.expanded_terms_total)
            except Exception as exc:  # noqa: BLE001
                logger.warning("search_audit_buffer.flush_failed: %s", exc)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="search-audit-buffer")

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
