"""Per-user rate limiter for the inline-completion typing path.

Dual fixed windows (Redis INCR + EXPIRE, the sprint-10
``PhraseWriteRateLimiter`` shape): a 1-second bucket caps the burst and
a 10-second bucket caps the sustained rate (~3 req/s). Fail-open on
Redis errors — a Redis outage must not freeze ghost text; the model
slot pool still bounds the damage a runaway client can do.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING
from uuid import UUID

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

_BURST_WINDOW_S = 1
_SUSTAINED_WINDOW_S = 10


class InlineRateLimiter:
    def __init__(self, redis: Redis, *, burst_per_second: int = 10, per_10s: int = 30) -> None:
        self._redis = redis
        self._burst = burst_per_second
        self._per_10s = per_10s

    async def check(self, *, user_id: UUID) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.time()
        burst_key = f"gen:inline-rl:{user_id}:s{int(now // _BURST_WINDOW_S)}"
        sustained_key = f"gen:inline-rl:{user_id}:t{int(now // _SUSTAINED_WINDOW_S)}"
        try:
            burst = await self._redis.incr(burst_key)
            if burst == 1:
                await self._redis.expire(burst_key, _BURST_WINDOW_S + 5)
            sustained = await self._redis.incr(sustained_key)
            if sustained == 1:
                await self._redis.expire(sustained_key, _SUSTAINED_WINDOW_S + 5)
        except Exception as exc:  # noqa: BLE001 — fail open
            logger.warning("generation.inline_rate_limit_redis_error: %s", exc)
            return True, 0
        if burst > self._burst:
            return False, 1
        if sustained > self._per_10s:
            return False, _SUSTAINED_WINDOW_S - int(now % _SUSTAINED_WINDOW_S)
        return True, 0
