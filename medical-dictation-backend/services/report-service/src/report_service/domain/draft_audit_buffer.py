"""Aggregates per-session ``report.draft.updated`` audit events.

Spec §5: emit one aggregated audit per dictation session, not one per
autosave. We buffer per ``(tenant_id, report_id, session_id)`` and
flush either when the dictation session signals end (sprint-04 hook)
or every 10 min.

Implementation: in-memory dict guarded by a single asyncio lock,
flushed by a background task that the service spawns at startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

logger = logging.getLogger(__name__)

_FLUSH_INTERVAL: Final = timedelta(minutes=10)


@dataclass(slots=True)
class _BufferedEntry:
    autosave_count: int = 0
    start_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    end_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    final_version_number: int = 0
    actor_user_id: UUID | None = None


FlushFn = Callable[
    [UUID, UUID, UUID | None, _BufferedEntry],
    Awaitable[None],
]


class DraftAuditBuffer:
    def __init__(
        self, *, flush_fn: FlushFn, max_flush_interval: timedelta = _FLUSH_INTERVAL
    ) -> None:
        self._flush_fn = flush_fn
        self._max_flush_interval = max_flush_interval
        self._lock = asyncio.Lock()
        self._buf: dict[tuple[UUID, UUID, UUID | None], _BufferedEntry] = {}
        self._task: asyncio.Task[None] | None = None

    async def record(
        self,
        *,
        tenant_id: UUID,
        report_id: UUID,
        dictation_session_id: UUID | None,
        actor_user_id: UUID,
        version_number: int,
    ) -> None:
        key = (tenant_id, report_id, dictation_session_id)
        now = datetime.now(UTC)
        async with self._lock:
            entry = self._buf.get(key)
            if entry is None:
                entry = _BufferedEntry(
                    autosave_count=1,
                    start_at=now,
                    end_at=now,
                    final_version_number=version_number,
                    actor_user_id=actor_user_id,
                )
                self._buf[key] = entry
            else:
                entry.autosave_count += 1
                entry.end_at = now
                entry.final_version_number = max(entry.final_version_number, version_number)

    async def flush_session(
        self, *, tenant_id: UUID, report_id: UUID, dictation_session_id: UUID
    ) -> None:
        key = (tenant_id, report_id, dictation_session_id)
        async with self._lock:
            entry = self._buf.pop(key, None)
        if entry is not None:
            try:
                await self._flush_fn(tenant_id, report_id, dictation_session_id, entry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("draft_audit_buffer.flush_failed: %s", exc)

    async def flush_all(self) -> None:
        async with self._lock:
            snapshot = list(self._buf.items())
            self._buf.clear()
        for (tid, rid, sid), entry in snapshot:
            try:
                await self._flush_fn(tid, rid, sid, entry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("draft_audit_buffer.flush_failed: %s", exc)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="draft-audit-buffer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await self._task
            self._task = None
        await self.flush_all()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._max_flush_interval.total_seconds())
            await self.flush_all()
