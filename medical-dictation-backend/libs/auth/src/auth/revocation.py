"""Session-revocation denylist (sprint 16 — closes the 15-minute window).

Threat model line item: "access tokens stay valid 15 min after logout".
The fix is a Redis denylist keyed by the token's ``sid`` claim (and, for
account-level kills, ``sub``), with TTL equal to the remaining token
lifetime — never longer, so the set is self-cleaning and can never grow
past one token-lifetime of churn.

Feed (ADR-0040): a Keycloak admin-events webhook needs a Keycloak SPI
extension deployed — infrastructure this sprint explicitly does not
touch — and admin-event polling would add latency and constant load.
Every session-ending path in this platform already flows through
auth-service (logout, refresh-replay force-logout, user deactivation),
so auth-service **pushes** revocations at the moment it performs them.
Keycloak is not publicly exposed in the production topology, so there is
no session-ending path that bypasses the push.

Fail-open by design (recorded tradeoff): if Redis is down, ``is_revoked``
returns False and logs a WARNING — availability over the residual
15-minute window, matching the house pattern for rate limiters. The
system degrades to exactly the pre-sprint-16 posture, never worse.

Verification is one Redis round-trip per request (pipelined EXISTS on
both keys); the push side is called a handful of times a day.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_SID_PREFIX = "mdx:revoked:sid:"
_SUB_PREFIX = "mdx:revoked:sub:"

# Floor for pushed TTLs: a token observed at the edge of its lifetime
# still gets denylisted long enough for clock skew.
_MIN_TTL_SECONDS = 30


class SessionDenylist(Protocol):
    """What ``build_current_user`` needs from a denylist."""

    async def is_revoked(self, *, sid: str, sub: str) -> bool: ...


class RedisSessionDenylist:
    """Redis-backed denylist. Safe for concurrent use; lazy connections.

    Construct via :func:`build_session_denylist` (settings-gated) or pass
    an existing ``redis.asyncio.Redis`` client to share a pool.
    """

    def __init__(self, *, redis: Any) -> None:
        self._redis = redis
        self._owns_client = False

    @classmethod
    def from_url(cls, url: str) -> RedisSessionDenylist:
        import redis.asyncio as aioredis

        inst = cls(redis=aioredis.from_url(url, decode_responses=False))
        inst._owns_client = True
        return inst

    async def aclose(self) -> None:
        if self._owns_client:
            await self._redis.aclose()

    # ── check side (every request) ──────────────────────────────────────
    async def is_revoked(self, *, sid: str, sub: str) -> bool:
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.exists(_SID_PREFIX + sid)
            pipe.exists(_SUB_PREFIX + sub)
            sid_hit, sub_hit = await pipe.execute()
            return bool(sid_hit) or bool(sub_hit)
        except Exception as exc:  # noqa: BLE001 — fail-OPEN, by decision
            logger.warning(
                "session_denylist.check_failed_fail_open",
                extra={"error": str(exc), "error_class": type(exc).__name__},
            )
            return False

    # ── push side (logout / deactivate / replay) ────────────────────────
    async def revoke_sid(self, sid: str, *, ttl_seconds: int) -> None:
        """Deny one session. TTL = remaining access-token lifetime."""
        await self._redis.set(
            _SID_PREFIX + sid, b"1", ex=max(int(ttl_seconds), _MIN_TTL_SECONDS)
        )

    async def revoke_sub(self, sub: str, *, ttl_seconds: int) -> None:
        """Deny every live session of a user (deactivation, replay)."""
        await self._redis.set(
            _SUB_PREFIX + sub, b"1", ex=max(int(ttl_seconds), _MIN_TTL_SECONDS)
        )

    async def clear_sub(self, sub: str) -> None:
        """Lift a user-level deny (reactivation before the TTL lapsed)."""
        await self._redis.delete(_SUB_PREFIX + sub)


def build_session_denylist(
    *, enabled: bool, redis_url: str
) -> RedisSessionDenylist | None:
    """The settings-gated composition helper every service uses.

    Returns ``None`` when the feature is off — ``build_current_user``
    treats a ``None`` denylist as "no check", which is bit-for-bit the
    pre-sprint-16 behaviour.
    """
    if not enabled:
        return None
    return RedisSessionDenylist.from_url(redis_url)
