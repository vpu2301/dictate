"""Per-user hourly rate limiter for corpus writes (Redis-backed).

Fixed-window INCR+EXPIRE bucket, same shape as the signing-service
public-verify limiter: a runaway SPA loop must not flood the corpus.
Fail-open on Redis errors — a Redis outage must not block a clinician
saving a phrase (the write path is already RLS- and PII-guarded).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING
from uuid import UUID

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

WINDOW_S = 3600


class PhraseWriteRateLimiter:
    def __init__(self, redis: Redis, *, per_hour: int = 100) -> None:
        self._redis = redis
        self._per_hour = per_hour

    async def check(self, *, user_id: UUID) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        bucket = int(time.time() // WINDOW_S)
        key = f"autocomplete:phrase-rl:{user_id}:{bucket}"
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, WINDOW_S + 60)
        except Exception as exc:  # noqa: BLE001 — fail open
            logger.warning("autocomplete.phrase_rate_limit_redis_error: %s", exc)
            return True, 0
        if count > self._per_hour:
            return False, WINDOW_S - int(time.time() % WINDOW_S)
        return True, 0
