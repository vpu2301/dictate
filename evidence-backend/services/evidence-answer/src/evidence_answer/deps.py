"""Auth dependencies + the dual-key scoped connection.

`questions` and `answers` carry user-private RLS (tenant AND owning user), so
their connections must set `app.user_id` on top of `app.tenant_id`. The GUC
name is the platform's (`app.user_id`, autocomplete-service precedent); the
*column* is `user_sub` — a naming mismatch worth remembering exactly once.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import asyncpg
from audit import Severity
from auth import AuthzDeniedError, Claims, JwksCache, build_current_user
from auth.perms import Action, TargetKind, check
from db import tenant_connection
from fastapi import Depends, HTTPException, status

from evidence_answer.config import settings
from evidence_answer.main_deps import get_state

logger = logging.getLogger(__name__)

_jwks_cache = JwksCache(issuer_to_url={settings.auth_issuer: settings.auth_jwks_url})

current_user = build_current_user(
    jwks_cache=_jwks_cache,
    expected_audience=settings.auth_audience,
    expected_issuer=settings.auth_issuer,
    clock_skew_seconds=settings.auth_clock_skew_seconds,
)


async def close_auth() -> None:
    await _jwks_cache.aclose()


def requires(
    action: Action, target_kind: TargetKind, *, scope: str | None = None
) -> Callable[..., Awaitable[Claims]]:
    async def dep(claims: Annotated[Claims, Depends(current_user)]) -> Claims:
        try:
            check(claims, action=action, target_kind=target_kind, scope=scope)
        except AuthzDeniedError as exc:
            try:
                await get_state().audit_writer.write_event(
                    tenant_id=exc.claims.tid,
                    kind="authz.denied",
                    actor_sub=exc.claims.sub,
                    actor_role=(exc.claims.roles[0] if exc.claims.roles else None),
                    target_kind=exc.target_kind,
                    payload={"action": exc.action, "reason": exc.reason},
                    severity=Severity.SEC,
                )
            except Exception as audit_exc:  # pragma: no cover — defensive
                logger.warning("authz_denied.audit_write_failed", extra={"error": str(audit_exc)})
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="permission denied"
            ) from exc
        return claims

    return dep


@asynccontextmanager
async def user_connection(
    pool: asyncpg.Pool, *, tenant_id: UUID, user_sub: UUID
) -> AsyncIterator[asyncpg.Connection]:
    """Tenant-scoped connection that also carries the owning user.

    Transaction-local like `app.tenant_id` (the `true` third argument), so it
    cannot leak into the next borrower of the pooled connection.
    """
    async with tenant_connection(pool, tenant_id) as conn:
        await conn.execute("SELECT set_config('app.user_id', $1, true)", str(user_sub))
        yield conn


def user_connection_factory(
    pool: asyncpg.Pool, *, tenant_id: UUID, user_sub: UUID
) -> Callable[[], AsyncIterator[asyncpg.Connection]]:
    def factory() -> AsyncIterator[asyncpg.Connection]:
        return user_connection(pool, tenant_id=tenant_id, user_sub=user_sub)  # type: ignore[return-value]

    return factory
