"""FastAPI dependency wiring (auth + RBAC), mirroring report-service."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from audit import Severity
from auth import Action, AuthzDeniedError, Claims, TargetKind, check

from .config import settings
from .main_deps import ServiceState

logger = logging.getLogger(__name__)

_state: ServiceState | None = None


def install_state(state: ServiceState) -> None:
    global _state
    _state = state


def get_state() -> ServiceState:
    if _state is None:
        raise RuntimeError(
            "ServiceState not installed; this code must run after lifespan startup"
        )
    return _state


async def current_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> Claims:
    """Extract and validate the JWT from the Authorization header."""
    state = get_state()
    if not hasattr(state, "_current_user_dep"):
        from auth import build_current_user

        state._current_user_dep = build_current_user(  # type: ignore[attr-defined]
            jwks_cache=state.jwks_cache,
            expected_audience=settings.auth_audience,
            expected_issuer=settings.auth_issuer,
            clock_skew_seconds=settings.auth_clock_skew_seconds,
        )
    dep = state._current_user_dep  # type: ignore[attr-defined]
    result: Claims = await dep(request, authorization)
    return result


def requires(
    action: Action, target_kind: TargetKind, *, scope: str | None = None
) -> Callable[..., Awaitable[Claims]]:
    """Create a dependency that enforces RBAC and audits denials.

    Usage::

        claims: Annotated[Claims, Depends(requires("patient.write", "patient"))]
    """

    async def dep(claims: Annotated[Claims, Depends(current_user)]) -> Claims:
        try:
            check(claims, action=action, target_kind=target_kind, scope=scope)
        except AuthzDeniedError as exc:
            state = _state
            if state is not None:
                try:
                    await state.audit_writer.write_event(
                        tenant_id=exc.claims.tid,
                        kind="authz.denied",
                        actor_sub=exc.claims.sub,
                        actor_role=(exc.claims.roles[0] if exc.claims.roles else None),
                        target_kind=exc.target_kind,
                        target_id=None,
                        payload={"action": exc.action, "reason": exc.reason},
                        severity=Severity.SEC,
                    )
                except Exception as audit_exc:  # pragma: no cover - defensive
                    logger.warning(
                        "authz_denied.audit_write_failed",
                        extra={"error": str(audit_exc)},
                    )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"deny: roles={list(claims.roles)} cannot "
                    f"{action!r} on {target_kind!r}"
                ),
            ) from exc
        return claims

    return dep
