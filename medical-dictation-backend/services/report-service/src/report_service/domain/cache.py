"""In-process TTL cache for templates.

The dictation hot path needs section-prompt lookups in < 10 ms p95;
re-fetching from Postgres on every Whisper window swap would burn the
budget. Cache key includes tenant_id so cross-tenant leakage is
impossible at the cache layer (RLS still gates the DB read, but the
cache must not blur tenants).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cachetools import TTLCache
from opentelemetry import metrics

logger = logging.getLogger(__name__)

_meter = metrics.get_meter("mdx.templates.cache")
_hits = _meter.create_counter("mdx_template_cache_hits_total", unit="1")
_misses = _meter.create_counter("mdx_template_cache_misses_total", unit="1")


@dataclass(frozen=True, slots=True)
class _CacheKey:
    tenant_id: UUID
    template_id: UUID


@dataclass(frozen=True, slots=True)
class CachedTemplate:
    template_id: UUID
    tenant_id: UUID | None
    schema_jsonb: dict[str, Any]
    schema_version: int
    status: str


class TemplateCache:
    """TTL-based cache keyed by (tenant_id, template_id).

    Templates are read-mostly; a 60-s TTL is the staleness budget for
    mid-session admin edits. ``invalidate`` clears on
    PUT /templates/{id}.
    """

    def __init__(self, *, maxsize: int = 5000, ttl_seconds: int = 60) -> None:
        self._cache: TTLCache[_CacheKey, CachedTemplate] = TTLCache(
            maxsize=maxsize, ttl=ttl_seconds
        )
        self._hits = 0
        self._calls = 0

    def get(self, *, tenant_id: UUID, template_id: UUID) -> CachedTemplate | None:
        self._calls += 1
        result = self._cache.get(_CacheKey(tenant_id, template_id))
        if result is not None:
            self._hits += 1
            _hits.add(1)
            return result
        _misses.add(1)
        return None

    def put(
        self,
        *,
        tenant_id: UUID,
        template_id: UUID,
        cached: CachedTemplate,
    ) -> None:
        self._cache[_CacheKey(tenant_id, template_id)] = cached

    def invalidate(self, *, tenant_id: UUID, template_id: UUID) -> None:
        self._cache.pop(_CacheKey(tenant_id, template_id), None)

    @property
    def hit_ratio(self) -> float:
        if self._calls == 0:
            return 0.0
        return self._hits / self._calls
