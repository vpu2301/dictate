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
        from auth import build_current_user, build_session_denylist

        state._current_user_dep = build_current_user(  # type: ignore[attr-defined]
            jwks_cache=state.jwks_cache,
            expected_audience=settings.auth_audience,
            expected_issuer=settings.auth_issuer,
            clock_skew_seconds=settings.auth_clock_skew_seconds,
            # Sprint 16: session-revocation denylist (None when the flag is
            # off — pre-sprint-16 behaviour, no Redis dependency at runtime).
            denylist=build_session_denylist(
                enabled=settings.session_revocation_enabled,
                redis_url=settings.redis_url,
            ),
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


def requires_mfa() -> Callable[..., Awaitable[Claims]]:
    """Sprint-16 MFA gate for the privacy surface (erasure approval, DSAR).

    Mirrors auth-service's grace flow: with ``MDX_REQUIRE_MFA`` on, a
    token without the ``mfa`` claim is rejected — 403 with machine code
    ``mfa_enrolment_required`` for unenrolled users (the FE routes to
    auth-service's enrolment), 401 with the MFA challenge for enrolled
    users holding a pre-enrolment token. Flag read per-call so tests and
    operators can flip it without a restart.
    """

    async def dep(claims: Annotated[Claims, Depends(current_user)]) -> Claims:
        if settings.require_mfa and not claims.mfa:
            if not claims.mfa_enrolled:
                exc = HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="MFA enrolment required for this endpoint",
                )
                exc.problem_extras = {"code": "mfa_enrolment_required"}  # type: ignore[attr-defined]
                raise exc
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MFA required for this endpoint",
                headers={"WWW-Authenticate": 'MFA realm="medical-dictation"'},
            )
        return claims

    return dep
