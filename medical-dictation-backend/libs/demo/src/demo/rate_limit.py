"""Sprint-07 demo-mode rate limiter.

Three-axis enforcement designed to be cheap (single Redis pipeline per
check) and forgiving (returns ``RateLimitBreach`` rather than raising
so callers can map to HTTP 429 with retry hints).

Axes:
- **per-IP**: max N concurrent sessions and max M dictation minutes
  per rolling 60 min window.
- **per-user**: max K dictation minutes per rolling 24h window.
- **per-session**: hard cap on a single session's wall-clock duration.

Counters are stored in Redis using TTL keys. The limiter is fail-OPEN
on Redis errors (logged) — the goal is the demo stays reachable; abuse
detection still flows through the audit trail.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis


BreachKind = Literal[
    "ip_concurrent",
    "ip_minutes",
    "user_minutes",
    "session_duration",
]


@dataclass(slots=True, frozen=True)
class RateLimitConfig:
    ip_concurrent_max: int = 3
    ip_minutes_per_hour: int = 30
    user_minutes_per_day: int = 60
    session_max_minutes: int = 15
    cooldown_after_hits: int = 5
    cooldown_seconds: int = 900  # 15 min


@dataclass(slots=True)
class RateLimitBreach:
    kind: BreachKind
    retry_after_seconds: int
    detail: str


class DemoRateLimiter:
    def __init__(self, redis: "Redis", config: RateLimitConfig | None = None) -> None:
        self._r = redis
        self._cfg = config or RateLimitConfig()

    async def begin_session(self, *, ip: str, user_id: str) -> RateLimitBreach | None:
        """Called from the auth/session-start path. Increments the
        per-IP concurrent counter; returns a breach if any axis is full.
        """
        # Cooldown check first — if the IP is in cooldown, deny fast.
        cd_key = f"demo:rl:cooldown:ip:{ip}"
        try:
            ttl = await self._r.ttl(cd_key)
        except Exception as exc:  # noqa: BLE001  fail-open on redis flakiness
            logger.warning("rate_limit redis error (begin): %s", exc)
            return None
        if ttl and ttl > 0:
            return RateLimitBreach(
                kind="ip_concurrent",
                retry_after_seconds=int(ttl),
                detail="ip in cooldown",
            )

        conc_key = f"demo:rl:ip:conc:{ip}"
        try:
            conc = await self._r.incr(conc_key)
            if conc == 1:
                await self._r.expire(conc_key, 3600)  # belt-and-braces TTL
        except Exception as exc:  # noqa: BLE001
            logger.warning("rate_limit redis error (incr): %s", exc)
            return None

        if conc > self._cfg.ip_concurrent_max:
            await self._record_hit(ip)
            try:
                await self._r.decr(conc_key)  # revert
            except Exception:  # noqa: BLE001
                pass
            return RateLimitBreach(
                kind="ip_concurrent",
                retry_after_seconds=60,
                detail=f"max {self._cfg.ip_concurrent_max} concurrent sessions per IP",
            )

        # Per-user daily window check (no increment yet — increment in
        # ``charge_minutes`` as wall-clock burns).
        user_minutes = await self._read_window(f"demo:rl:user:min:{user_id}", 86400)
        if user_minutes >= self._cfg.user_minutes_per_day:
            return RateLimitBreach(
                kind="user_minutes",
                retry_after_seconds=3600,
                detail=f"daily minute budget exhausted ({self._cfg.user_minutes_per_day}/24h)",
            )

        return None

    async def end_session(self, *, ip: str) -> None:
        try:
            await self._r.decr(f"demo:rl:ip:conc:{ip}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("rate_limit redis error (end): %s", exc)

    async def charge_minutes(
        self, *, ip: str, user_id: str, minutes: float
    ) -> RateLimitBreach | None:
        """Called periodically (e.g. every 30s of streaming) and at session end."""
        cents = max(1, int(minutes * 100))
        ip_key = f"demo:rl:ip:min:{ip}"
        user_key = f"demo:rl:user:min:{user_id}"
        try:
            pipe = self._r.pipeline()
            pipe.incrby(ip_key, cents)
            pipe.expire(ip_key, 3600, nx=True)
            pipe.incrby(user_key, cents)
            pipe.expire(user_key, 86400, nx=True)
            ip_total_cents, _, user_total_cents, _ = await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("rate_limit redis error (charge): %s", exc)
            return None

        ip_total = ip_total_cents / 100.0
        user_total = user_total_cents / 100.0

        if ip_total >= self._cfg.ip_minutes_per_hour:
            await self._record_hit(ip)
            return RateLimitBreach(
                kind="ip_minutes",
                retry_after_seconds=3600,
                detail=f"ip burned {ip_total:.1f}/{self._cfg.ip_minutes_per_hour} min/h",
            )
        if user_total >= self._cfg.user_minutes_per_day:
            return RateLimitBreach(
                kind="user_minutes",
                retry_after_seconds=86400,
                detail=f"user burned {user_total:.1f}/{self._cfg.user_minutes_per_day} min/24h",
            )
        return None

    async def session_too_long(self, *, started_at_unix: float) -> RateLimitBreach | None:
        elapsed_min = (time.time() - started_at_unix) / 60.0
        if elapsed_min >= self._cfg.session_max_minutes:
            return RateLimitBreach(
                kind="session_duration",
                retry_after_seconds=0,
                detail=f"session reached {self._cfg.session_max_minutes} min cap",
            )
        return None

    # ── internals ───────────────────────────────────────────────────

    async def _record_hit(self, ip: str) -> None:
        hits_key = f"demo:rl:ip:hits:{ip}"
        try:
            hits = await self._r.incr(hits_key)
            if hits == 1:
                await self._r.expire(hits_key, 3600)
            if hits >= self._cfg.cooldown_after_hits:
                await self._r.setex(
                    f"demo:rl:cooldown:ip:{ip}", self._cfg.cooldown_seconds, "1"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("rate_limit redis error (hit): %s", exc)

    async def _read_window(self, key: str, _ttl_unused: int) -> float:
        try:
            v = await self._r.get(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rate_limit redis error (read): %s", exc)
            return 0.0
        if v is None:
            return 0.0
        try:
            return float(int(v)) / 100.0
        except (TypeError, ValueError):
            return 0.0
