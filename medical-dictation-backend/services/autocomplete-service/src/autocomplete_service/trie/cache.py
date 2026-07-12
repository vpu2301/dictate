"""Redis-backed trie cache with per-key lock + version-tag invalidation.

Key shape:
    autocomplete:trie:{tenant_id}:{language}:{user_id}

A per-tenant ``version_tag`` is also stored:
    autocomplete:tenant_phrase_version:{tenant_id}

On every phrase write or roll-up, the writer increments the
version_tag. Readers compare the tag stored alongside the cached blob
and rebuild if mismatched. This avoids explicit DELs (no cache
stampede when the tag flips).

Per-key lock (``SET NX EX 10``) prevents thundering herd when 100
parallel readers all see a cold cache. Loser-of-lock waits up to
200 ms (polled), then falls back to a direct build WITHOUT writing the
cache.

Failure doctrine: Redis being down must never take suggest down. Every
Redis interaction is guarded; any Redis error routes to the degraded
path (direct DB build, no caching) with a throttled warning and the
``mdx_autocomplete_degraded_total`` counter — the endpoint always
answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from autocomplete_service.trie.builder import TenantTrie
from autocomplete_service.trie.serializer import (
    SerializerVersionMismatchError,
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
REDIS_WARN_EVERY_S = 30.0


class TrieCache:
    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int = 3600,
        degraded_metric: Any = None,
        build_seconds_metric: Any = None,
        size_bytes_metric: Any = None,
    ) -> None:
        self._r = redis
        self._ttl = ttl_seconds
        self._degraded_metric = degraded_metric
        self._build_seconds = build_seconds_metric
        self._size_bytes = size_bytes_metric
        self._last_redis_warn = 0.0

    # ── failure plumbing ─────────────────────────────────────────────

    def _warn_throttled(self, msg: str, **extra: Any) -> None:
        now = time.monotonic()
        if now - self._last_redis_warn >= REDIS_WARN_EVERY_S:
            self._last_redis_warn = now
            logger.warning(msg, extra=extra)

    async def _degraded(
        self,
        build_fn: Callable[[], Awaitable[TenantTrie]],
        *,
        tenant_id: UUID,
        reason: str,
    ) -> tuple[TenantTrie, str]:
        self._warn_throttled(
            f"trie_cache.{reason}", tenant_id=str(tenant_id)
        )
        if self._degraded_metric is not None:
            self._degraded_metric.add(1, {"reason": reason})
        return await build_fn(), "degraded"

    async def _timed_build(
        self, build_fn: Callable[[], Awaitable[TenantTrie]]
    ) -> TenantTrie:
        t0 = time.monotonic()
        trie = await build_fn()
        if self._build_seconds is not None:
            self._build_seconds.record(time.monotonic() - t0)
        return trie

    # ── the hot path ─────────────────────────────────────────────────

    async def get_or_build(
        self,
        *,
        tenant_id: UUID,
        language: str,
        user_id: UUID,
        build_fn: Callable[[], Awaitable[TenantTrie]],
    ) -> tuple[TenantTrie, str]:
        """Returns (trie, status) — status ∈ {"hit", "miss", "degraded"}.

        On miss: tries to acquire the per-key lock; on win, builds and
        stores; on loss, polls the cache briefly then falls back to
        ``build_fn()`` directly (degraded mode). On ANY Redis error:
        degraded mode — suggest answers with Redis down.
        """
        key = _TRIE_KEY.format(tid=tenant_id, lang=language, uid=user_id)
        tag_key = _TAG_KEY.format(tid=tenant_id)

        try:
            cached = await self._r.get(key)
            current_tag = await self._r.get(tag_key)
        except Exception:  # noqa: BLE001 — any Redis failure → degrade
            return await self._degraded(
                build_fn, tenant_id=tenant_id, reason="redis_unavailable_degraded"
            )

        # A tenant whose vtag key does not exist yet (nothing ever INCR'd it)
        # is at implicit version "0" — the same value the build stores below.
        # Without this, such tenants NEVER hit the cache and every keystroke
        # rebuilds the trie (found by the step-08 load run: 20k misses where
        # ~6 were expected; hit-path p95 227 ms).
        if current_tag is None:
            current_tag = b"0"

        if cached:
            try:
                trie = deserialize_trie(
                    cached if isinstance(cached, bytes) else cached.encode("latin-1")
                )
                stored_tag = await self._r.get(key + ":tag")
                if stored_tag == current_tag:
                    return trie, "hit"
            except SerializerVersionMismatchError:
                # Stale format / corrupt blob → fall through to rebuild.
                pass
            except Exception:  # noqa: BLE001 — Redis died mid-read
                return await self._degraded(
                    build_fn, tenant_id=tenant_id, reason="redis_unavailable_degraded"
                )

        lock_key = _LOCK_KEY.format(tid=tenant_id, lang=language, uid=user_id)
        try:
            got_lock = await self._r.set(lock_key, b"1", nx=True, ex=LOCK_TTL)
        except Exception:  # noqa: BLE001
            return await self._degraded(
                build_fn, tenant_id=tenant_id, reason="redis_unavailable_degraded"
            )

        if got_lock:
            try:
                trie = await self._timed_build(build_fn)
                blob = serialize_trie(trie)
                if self._size_bytes is not None:
                    self._size_bytes.record(len(blob))
                try:
                    pipe = self._r.pipeline()
                    pipe.set(key, blob, ex=self._ttl)
                    tag = current_tag or b"0"
                    tag_str = tag.decode() if isinstance(tag, bytes) else str(tag)
                    pipe.set(key + ":tag", tag_str, ex=self._ttl)
                    await pipe.execute()
                except Exception:  # noqa: BLE001 — cache write is best-effort
                    self._warn_throttled(
                        "trie_cache.write_failed_serving_uncached",
                        tenant_id=str(tenant_id),
                    )
                return trie, "miss"
            finally:
                # If Redis dies here the lock simply EXpires in 10 s.
                with contextlib.suppress(Exception):
                    await self._r.delete(lock_key)

        # Lost the race: poll briefly for the populated cache.
        deadline = time.monotonic() + LOCK_WAIT_MAX_S
        while time.monotonic() < deadline:
            await asyncio.sleep(LOCK_POLL_INTERVAL_S)
            try:
                cached = await self._r.get(key)
            except Exception:  # noqa: BLE001
                break
            if cached:
                try:
                    return deserialize_trie(
                        cached if isinstance(cached, bytes) else cached.encode("latin-1")
                    ), "hit"
                except SerializerVersionMismatchError:
                    pass

        # Degraded: build directly without populating the cache (the lock
        # winner owns the write; next call reads it). Never block a
        # keystroke on another request's build beyond the 200 ms poll.
        return await self._degraded(
            build_fn, tenant_id=tenant_id, reason="lock_lost_degraded_fallback"
        )

    async def bump_version_tag(self, *, tenant_id: UUID) -> None:
        tag_key = _TAG_KEY.format(tid=tenant_id)
        try:
            await self._r.incr(tag_key)
        except Exception:  # noqa: BLE001 — staleness is bounded by the TTL
            self._warn_throttled(
                "trie_cache.vtag_bump_failed", tenant_id=str(tenant_id)
            )
