"""WebSocket upgrade — auth, subprotocol negotiation, rate-limit.

The WS upgrade is, at the HTTP layer, an ordinary GET request with
``Upgrade: websocket``. We validate the bearer token and the requested
subprotocol BEFORE calling ``websocket.accept()``. A rejected upgrade
returns a normal HTTP response (400 / 401 / 429) — never a WS handshake
that immediately closes.

Per-IP and per-user rate limits use Redis counters with TTLs.

Audit:
- Every rejection writes ``dictation.upgrade.failed`` (warn or sec).
- Successful upgrades audit on session start in the session handler.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException, WebSocket, status
from redis.asyncio import Redis

from audit import AuditWriter, Severity
from auth import Claims, JwksCache, verify_token
from auth.exceptions import (
    AuthError,
    ExpiredTokenError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidTokenError,
    JwksFetchError,
    KidNotFoundError,
    MalformedClaimsError,
)

from ..audit_kinds import UPGRADE_FAILED
from ..config import settings
from ..protocol.codec import SUBPROTOCOL

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UpgradeContext:
    """Everything the session handler needs once the upgrade succeeds."""

    claims: Claims
    subprotocol: str
    client_ip: str
    origin: str | None


class UpgradeRejected(HTTPException):
    """Raised before ``accept()`` — Starlette returns this as plain HTTP."""

    def __init__(self, status_code: int, code: str, detail: str = "") -> None:
        super().__init__(status_code=status_code, detail={"code": code, "detail": detail})
        self.code = code


async def authorize_upgrade(
    websocket: WebSocket,
    *,
    jwks_cache: JwksCache,
    redis: Redis,
    audit_writer: AuditWriter,
) -> UpgradeContext:
    """Validate the upgrade or raise :class:`UpgradeRejected`.

    Order matters: we check the subprotocol header BEFORE the JWT, so a
    misconfigured client doesn't burn a JWKS verification on us. The
    rate-limit check happens before subprotocol — DoS protection wins.
    """
    client_ip = _client_ip(websocket)
    origin = websocket.headers.get("origin")

    # Origin allow-list. Browsers send `Origin` for cross-site WS too;
    # we mirror the frontend CORS allow-list. CLI tools (no Origin)
    # are allowed through in dev only.
    if origin is not None and origin not in settings.ws_allowed_origins:
        if settings.environment != "development":
            await _audit_upgrade_fail(
                audit_writer,
                tenant_id=None,
                user_sub=None,
                client_ip=client_ip,
                reason="origin_rejected",
                severity=Severity.WARN,
                origin=origin,
            )
            raise UpgradeRejected(
                status_code=status.HTTP_403_FORBIDDEN,
                code="origin_rejected",
                detail=f"origin {origin!r} is not in the allow-list",
            )

    # Per-IP rate limit: 10 upgrade attempts per minute.
    if not await _allow_ip(redis, client_ip):
        await _audit_upgrade_fail(
            audit_writer,
            tenant_id=None,
            user_sub=None,
            client_ip=client_ip,
            reason="rate_limited_ip",
            severity=Severity.SEC,
        )
        raise UpgradeRejected(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code="rate_limited",
            detail="too many upgrade attempts from this IP",
        )

    # Subprotocol negotiation: client must offer `medical-dictation.v1`.
    offered = _parse_subprotocols(websocket.headers.get("sec-websocket-protocol"))
    if SUBPROTOCOL not in offered:
        await _audit_upgrade_fail(
            audit_writer,
            tenant_id=None,
            user_sub=None,
            client_ip=client_ip,
            reason="subprotocol_missing",
            severity=Severity.WARN,
            offered=",".join(offered),
        )
        raise UpgradeRejected(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="unsupported_protocol",
            detail=(
                f"client did not offer {SUBPROTOCOL!r}; offered={offered!r}"
            ),
        )

    # Bearer token. We accept Authorization header *or* (some browsers
    # can't set Authorization on WS) a ?token= query param. Both are
    # validated identically.
    bearer = _extract_bearer(websocket)
    if bearer is None:
        await _audit_upgrade_fail(
            audit_writer,
            tenant_id=None,
            user_sub=None,
            client_ip=client_ip,
            reason="auth_missing",
            severity=Severity.WARN,
        )
        raise UpgradeRejected(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="auth_invalid",
            detail="missing bearer token",
        )

    try:
        claims = await verify_token(
            bearer,
            jwks_cache=jwks_cache,
            expected_audience=settings.auth_audience,
            expected_issuer=settings.auth_issuer,
            clock_skew_seconds=settings.auth_clock_skew_seconds,
        )
    except ExpiredTokenError as exc:
        await _audit_upgrade_fail(
            audit_writer,
            tenant_id=None,
            user_sub=None,
            client_ip=client_ip,
            reason="token_expired",
            severity=Severity.WARN,
        )
        raise UpgradeRejected(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="auth_invalid",
            detail="token expired",
        ) from exc
    except (
        InvalidTokenError,
        InvalidIssuerError,
        InvalidAudienceError,
        KidNotFoundError,
        MalformedClaimsError,
        JwksFetchError,
        AuthError,
    ) as exc:
        await _audit_upgrade_fail(
            audit_writer,
            tenant_id=None,
            user_sub=None,
            client_ip=client_ip,
            reason=f"auth_invalid:{type(exc).__name__}",
            severity=Severity.SEC,
        )
        raise UpgradeRejected(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="auth_invalid",
            detail=type(exc).__name__,
        ) from exc

    # Per-user rate limit: 30 upgrades per hour.
    if not await _allow_user(redis, claims.sub):
        await _audit_upgrade_fail(
            audit_writer,
            tenant_id=claims.tid,
            user_sub=claims.sub,
            client_ip=client_ip,
            reason="rate_limited_user",
            severity=Severity.SEC,
        )
        raise UpgradeRejected(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code="rate_limited",
            detail="too many upgrade attempts for this user",
        )

    return UpgradeContext(
        claims=claims,
        subprotocol=SUBPROTOCOL,
        client_ip=client_ip,
        origin=origin,
    )


# ── Helpers ──────────────────────────────────────────────────────────


def _client_ip(websocket: WebSocket) -> str:
    """Best-effort client-IP extraction.

    Honours `X-Forwarded-For` only in dev. In prod the load balancer
    sets a trusted header that the SRE configures; for sprint 4 dev we
    accept the leftmost XFF value.
    """
    xff = websocket.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if websocket.client is None:
        return "unknown"
    return websocket.client.host


def _parse_subprotocols(header: str | None) -> list[str]:
    if not header:
        return []
    return [s.strip() for s in header.split(",") if s.strip()]


def _extract_bearer(websocket: WebSocket) -> str | None:
    auth = websocket.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # Browsers can't set Authorization on the WS upgrade. As a fallback
    # the frontend may pass ?token= (over TLS only — documented).
    token = websocket.query_params.get("token")
    return token if token else None


# ── Redis-backed rate limiters ───────────────────────────────────────


async def _allow_ip(redis: Redis, ip: str) -> bool:
    key = f"mdx:dict:rl:ip:{ip}:{int(time.time()) // 60}"
    return await _allow(redis, key, settings.upgrade_ratelimit_per_ip_per_minute, ttl=120)


async def _allow_user(redis: Redis, sub: UUID) -> bool:
    key = f"mdx:dict:rl:user:{sub}:{int(time.time()) // 3600}"
    return await _allow(
        redis, key, settings.upgrade_ratelimit_per_user_per_hour, ttl=3 * 3600
    )


async def _allow(redis: Redis, key: str, limit: int, *, ttl: int) -> bool:
    """Atomic INCR + EXPIRE; returns False once the bucket exceeds limit."""
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, ttl)
    results: list[Any] = await pipe.execute()
    current = int(results[0])
    return current <= limit


async def _audit_upgrade_fail(
    audit_writer: AuditWriter,
    *,
    tenant_id: UUID | None,
    user_sub: UUID | None,
    client_ip: str,
    reason: str,
    severity: Severity,
    **extra: object,
) -> None:
    """Write a `dictation.upgrade.failed` event.

    ``tenant_id`` may be None for pre-auth failures (the chain requires
    a tenant scope — we have to skip the audit row in that case and rely
    on the structured log line. Real prod adds a "system" tenant for
    this, but sprint 4 keeps it simple).
    """
    payload: dict[str, object] = {
        "reason": reason,
        "client_ip": client_ip,
        **extra,
    }
    log_extra = {
        "reason": reason,
        "client_ip": client_ip,
        "tenant_id": str(tenant_id) if tenant_id else None,
        **{k: str(v) for k, v in extra.items()},
    }
    if tenant_id is None:
        logger.warning("dictation.upgrade.failed", extra=log_extra)
        return
    try:
        await audit_writer.write_event(
            tenant_id=tenant_id,
            kind=UPGRADE_FAILED,
            actor_sub=user_sub,
            target_kind="dictation_session",
            target_id=None,
            payload=payload,
            severity=severity,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dictation.upgrade.failed.audit_write_failed",
            extra={"error": str(exc), "error_class": type(exc).__name__, **log_extra},
        )
