"""Per-user hourly clip-creation limiter (Redis fixed window, fail-open).

Replay is review, not export: 30 clips/user/hour keeps the endpoint
useless for bulk PHI extraction while never getting in the way of a
clinician spot-checking a note. Same INCR+EXPIRE shape as the sprint-10
phrase-write limiter.
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


class ClipRateLimiter:
    def __init__(self, redis: Redis, *, per_hour: int = 30) -> None:
        self._redis = redis
        self._per_hour = per_hour

    async def check(self, *, user_id: UUID) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        bucket = int(time.time() // WINDOW_S)
        key = f"report:clip-rl:{user_id}:{bucket}"
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, WINDOW_S + 60)
        except Exception as exc:  # noqa: BLE001 — fail open
            logger.warning("report.clip_rate_limit_redis_error: %s", exc)
            return True, 0
        if count > self._per_hour:
            return False, WINDOW_S - int(time.time() % WINDOW_S)
        return True, 0
