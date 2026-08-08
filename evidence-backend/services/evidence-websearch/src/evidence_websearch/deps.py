"""Auth dependencies: platform tokens (rule I1) + requires() with audited
denials (evidence-ingest / core-service pattern)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

from audit import Severity
from auth import AuthzDeniedError, Claims, JwksCache, build_current_user
from auth.perms import Action, TargetKind, check
from fastapi import Depends, HTTPException, status

from evidence_websearch.config import settings
from evidence_websearch.main_deps import get_state

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
