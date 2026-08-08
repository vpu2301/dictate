"""Intake: authorization has already happened at the router; this stage owns
the concurrency cap.

Two answers in flight per user (spec NFR). The cap is a Redis counter with a
TTL, not an in-process semaphore, because the service runs multiple replicas
and a per-process cap would multiply by the replica count.

The TTL is the safety net: a worker that dies mid-answer would otherwise leak
a slot forever. It is set generously (a full pipeline budget plus slack) so it
never releases a slot that is still genuinely in use.
"""

from __future__ import annotations

import logging
from types import TracebackType
from uuid import UUID

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class PipelineOverloadedError(Exception):
    """The caller already has the maximum number of answers in flight."""


class InFlightSlot:
    """Async context manager holding one in-flight slot for a user."""

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        tenant_id: UUID,
        user_sub: UUID,
        limit: int,
        ttl_seconds: int,
    ) -> None:
        self._redis = redis
        self._key = f"eva:answer:inflight:{tenant_id}:{user_sub}"
        self._limit = limit
        self._ttl = ttl_seconds
        self._held = False

    async def __aenter__(self) -> InFlightSlot:
        count = await self._redis.incr(self._key)
        # Refresh on every acquire, not just the first: a long-running second
        # answer must not inherit the first one's remaining TTL.
        await self._redis.expire(self._key, self._ttl)
        if count > self._limit:
            await self._redis.decr(self._key)
            raise PipelineOverloadedError(
                f"{count - 1} answers already in flight (limit {self._limit})"
            )
        self._held = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._held:
            return
        self._held = False
        try:
            remaining = await self._redis.decr(self._key)
            if remaining <= 0:
                await self._redis.delete(self._key)
        except Exception as release_exc:  # pragma: no cover — defensive
            # A leaked slot expires with the TTL; failing the response here
            # would turn a bookkeeping hiccup into a user-visible error.
            logger.warning(
                "intake.slot_release_failed", extra={"error": type(release_exc).__name__}
            )
