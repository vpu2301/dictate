"""Tiny in-process LRU cache for diff responses.

Versions are immutable so cache hits are always safe. Key:
``(report_id, from_id, to_id)``. Production deployments can swap to a
Redis-backed cache later — the interface is intentionally simple.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from report_models import DiffResponse


@dataclass(slots=True)
class _Entry:
    response: DiffResponse


class DiffCache:
    def __init__(self, *, max_entries: int = 1024) -> None:
        self._cache: OrderedDict[tuple[UUID, UUID, UUID], _Entry] = OrderedDict()
        self._max = max_entries
        self.hits = 0
        self.misses = 0

    def get(self, *, report_id: UUID, from_id: UUID, to_id: UUID) -> DiffResponse | None:
        key = (report_id, from_id, to_id)
        entry = self._cache.get(key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        # Bump to MRU.
        self._cache.move_to_end(key)
        # Defensive copy of the cached flag.
        return entry.response.model_copy(update={"cached": True})

    def put(self, *, report_id: UUID, from_id: UUID, to_id: UUID, value: DiffResponse) -> None:
        key = (report_id, from_id, to_id)
        self._cache[key] = _Entry(response=value)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total > 0 else 0.0
