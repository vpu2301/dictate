"""Per-draft autosave rate limiter.

Spec §3 day-3: 1 PUT per 5s per draft. Pure in-memory; one process per
service replica — coordination between replicas is not needed because
optimistic-lock collisions handle the cross-replica race already.

This protects against:
- Misbehaving FE that fires autosaves much faster than the 30s/50-key
  contract.
- Loops triggered by a bug.

429 emits an audit event (router layer), and the metric
``mdx_reports_autosave_rate_limit_hits_total`` increments.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID


@dataclass(slots=True)
class _Slot:
    last_ts: float = 0.0


class AutosaveRateLimiter:
    def __init__(self, *, min_interval_s: float = 5.0) -> None:
        self._min_interval = min_interval_s
        self._slots: dict[UUID, _Slot] = defaultdict(_Slot)
        self._lock = asyncio.Lock()

    async def check_and_record(self, *, report_id: UUID) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        async with self._lock:
            slot = self._slots[report_id]
            now = time.monotonic()
            elapsed = now - slot.last_ts
            if elapsed < self._min_interval:
                return False, max(1, int(self._min_interval - elapsed))
            slot.last_ts = now
        return True, 0

    def reset(self, report_id: UUID) -> None:
        self._slots.pop(report_id, None)
