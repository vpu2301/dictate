"""Abuse caps for the unauthenticated password-recovery endpoints.

Same fixed-window Redis shape every other service in this repo uses
(``signing_service.rate_limit``, ``autocomplete_service.rate_limit``,
``generation_service.domain.rate_limit``) and, like all of them,
**fail-open**: if Redis is down, requests are allowed and a warning is
logged. Locking every user out of account recovery because a cache is
unavailable would be a worse outage than the abuse the limiter prevents.

Two independent windows, because they stop different things:

  * **per IP** — one host sweeping many addresses to find which ones
    have accounts, or to generate mail volume from our domain.
  * **per email** — many hosts (or one behind a proxy pool) aimed at a
    single mailbox. Without this, a botnet can flood one person's inbox
    with reset mail until they stop reading it, which is a real
    technique for hiding the one notification that matters.

The email key is hashed, not stored raw. Redis here is shared
infrastructure with its own operational access, and a key namespace
that enumerates every address that ever asked for a reset is a user
list waiting to be dumped.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

_WINDOW_SECONDS = 3600
# Outlives the window so a bucket cannot be reset by racing its own expiry.
_KEY_TTL = _WINDOW_SECONDS + 60


class PasswordResetRateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        ip_per_hour: int = 20,
        email_per_hour: int = 5,
        email_salt: str = "",
    ) -> None:
        self._redis = redis
        self._ip_per_hour = ip_per_hour
        self._email_per_hour = email_per_hour
        self._email_salt = email_salt

    def _email_key(self, email: str, bucket: int) -> str:
        digest = hashlib.sha256(
            f"{self._email_salt}:{email.strip().lower()}".encode()
        ).hexdigest()[:32]
        return f"auth:pwreset-rl:email:{digest}:{bucket}"

    async def _bump(self, key: str, limit: int) -> bool:
        """Increment one window. True = still within the cap."""
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, _KEY_TTL)
        except Exception as exc:  # noqa: BLE001 — fail-OPEN, by decision
            logger.warning(
                "auth.password_reset.rate_limit_redis_error",
                extra={"error": str(exc), "error_class": type(exc).__name__},
            )
            return True
        return int(count) <= limit

    async def check(self, *, ip: str, email: str) -> bool:
        """True when the request may proceed.

        Both windows are bumped even when the first one already refused.
        Short-circuiting would let an attacker who has exhausted the IP
        budget keep hammering one mailbox for free once they rotate
        address — the per-email counter has to see every attempt to be
        the control it is meant to be.
        """
        bucket = int(time.time() // _WINDOW_SECONDS)
        ip_ok = True
        if ip:
            ip_ok = await self._bump(f"auth:pwreset-rl:ip:{ip}:{bucket}", self._ip_per_hour)
        email_ok = await self._bump(
            self._email_key(email, bucket), self._email_per_hour
        )
        return ip_ok and email_ok
