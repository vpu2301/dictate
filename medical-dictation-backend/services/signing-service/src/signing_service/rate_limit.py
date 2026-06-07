"""Token-bucket rate limiter for public /verify (Redis-backed).

Bucket: per remote IP, 60 requests / minute (configurable). 429 with
``Retry-After`` on overflow.

Fail-open on Redis errors — the public endpoint must stay reachable
for legitimate verifiers (courts, regulators).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis


class PublicVerifyRateLimiter:
    def __init__(self, redis: Redis, *, per_minute: int = 60) -> None:
        self._redis = redis
        self._per_minute = per_minute

    async def check(self, *, ip: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        bucket = int(time.time() // 60)
        key = f"public-verify:rl:{ip}:{bucket}"
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, 70)
        except Exception as exc:  # noqa: BLE001
            logger.warning("public_verify.rate_limit_redis_error: %s", exc)
            return True, 0
        if count > self._per_minute:
            return False, 60 - int(time.time() % 60)
        return True, 0
