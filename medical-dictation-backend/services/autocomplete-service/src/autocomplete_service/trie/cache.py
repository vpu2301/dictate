"""Redis-backed trie cache with per-key lock + version-tag invalidation.

Key shape:
    autocomplete:trie:{tenant_id}:{language}:{user_id}

A per-tenant ``version_tag`` is also stored:
    autocomplete:tenant_phrase_version:{tenant_id}

On every phrase write or roll-up, the writer increments the
version_tag. Readers compare the tag baked into the cached bytes (in
the header section) and rebuild if mismatched. This avoids
explicit DELs (no cache stampede when the tag flips).

Per-key lock (``SET NX EX 10``) prevents thundering herd when 100
parallel readers all see a cold cache. Loser-of-lock waits up to
200 ms (polled), then either reads the now-populated cache or falls
back to a direct DB query.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable
from uuid import UUID

from autocomplete_service.trie.builder import TenantTrie
from autocomplete_service.trie.serializer import (
    SerializerVersionMismatch,
    deserialize_trie,
    serialize_trie,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis


_TRIE_KEY = "autocomplete:trie:{tid}:{lang}:{uid}"
_TAG_KEY = "autocomplete:tenant_phrase_version:{tid}"
_LOCK_KEY = "autocomplete:trie:{tid}:{lang}:{uid}:lock"

LOCK_TTL = 10
LOCK_WAIT_MAX_S = 0.20
LOCK_POLL_INTERVAL_S = 0.02


class TrieCache:
    def __init__(self, redis: "Redis", *, ttl_seconds: int = 3600) -> None:
        self._r = redis
        self._ttl = ttl_seconds

    async def get_or_build(
        self,
        *,
        tenant_id: UUID,
        language: str,
        user_id: UUID,
        build_fn: Callable[[], Awaitable[TenantTrie]],
    ) -> tuple[TenantTrie, bool]:
        """Returns (trie, cache_hit).

        On miss: tries to acquire the per-key lock; on win, builds and
        stores; on loss, polls the cache briefly then falls back to
        ``build_fn()`` directly (degraded mode).
        """
        key = _TRIE_KEY.format(tid=tenant_id, lang=language, uid=user_id)
        tag_key = _TAG_KEY.format(tid=tenant_id)

        cached = await self._r.get(key)
        current_tag = await self._r.get(tag_key)
        if cached and current_tag:
            try:
                trie = deserialize_trie(cached if isinstance(cached, bytes) else cached.encode("latin-1"))
                stored_tag = await self._r.get(key + ":tag")
                if stored_tag == current_tag:
                    return trie, True
            except SerializerVersionMismatch:
                # Stale format → fall through to rebuild.
                pass

        lock_key = _LOCK_KEY.format(tid=tenant_id, lang=language, uid=user_id)
        got_lock = await self._r.set(lock_key, b"1", nx=True, ex=LOCK_TTL)
        if got_lock:
            try:
                trie = await build_fn()
                blob = serialize_trie(trie)
                pipe = self._r.pipeline()
                pipe.set(key, blob, ex=self._ttl)
                tag = current_tag or b"0"
                tag_str = tag.decode() if isinstance(tag, bytes) else str(tag)
                pipe.set(key + ":tag", tag_str, ex=self._ttl)
                await pipe.execute()
                return trie, False
            finally:
                await self._r.delete(lock_key)

        # Lost the race: poll briefly for the populated cache.
        deadline = time.monotonic() + LOCK_WAIT_MAX_S
        while time.monotonic() < deadline:
            await asyncio.sleep(LOCK_POLL_INTERVAL_S)
            cached = await self._r.get(key)
            if cached:
                try:
                    return deserialize_trie(
                        cached if isinstance(cached, bytes) else cached.encode("latin-1")
                    ), True
                except SerializerVersionMismatch:
                    pass

        # Degraded: build directly without populating cache (next call retries).
        logger.warning("trie_cache.lock_lost_degraded_fallback",
                       extra={"tenant_id": str(tenant_id)})
        trie = await build_fn()
        return trie, False

    async def bump_version_tag(self, *, tenant_id: UUID) -> None:
        tag_key = _TAG_KEY.format(tid=tenant_id)
        await self._r.incr(tag_key)
