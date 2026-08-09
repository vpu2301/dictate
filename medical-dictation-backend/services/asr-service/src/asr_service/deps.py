"""FastAPI dependency wiring for asr-service.

Mirrors the auth-service pattern: a module-level ``ServiceState`` filled
during the lifespan, plus thin Depends-callable wrappers.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from opentelemetry import metrics

from audit import Severity
from auth import Action, AuthzDeniedError, Claims, TargetKind, check, check_any

from .main_deps import ServiceState

logger = logging.getLogger(__name__)

_meter = metrics.get_meter("mdx.asr.authz")
_authz_denied_counter = _meter.create_counter(
    "mdx_authz_denied_total",
    description="Times asr-service requires() rejected a caller",
    unit="1",
)

_state: ServiceState | None = None


def install_state(state: ServiceState) -> None:
    global _state
    _state = state


def get_state() -> ServiceState:
    if _state is None:
        raise RuntimeError("ServiceState not installed; this code must run after lifespan startup")
    return _state


async def current_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> Claims:
    state = get_state()
    if not hasattr(state, "_current_user_dep"):
        from auth import build_current_user, build_session_denylist

        from .config import settings

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
    """Return a FastAPI dependency that enforces the perms matrix."""

    async def dep(claims: Annotated[Claims, Depends(current_user)]) -> Claims:
        try:
            check(claims, action=action, target_kind=target_kind, scope=scope)
        except AuthzDeniedError as exc:
            await _emit_authz_denied(exc)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(f"deny: roles={list(claims.roles)} cannot {action!r} on {target_kind!r}"),
            ) from exc
        return claims

    return dep


def requires_any(
    *options: tuple[Action, TargetKind],
) -> Callable[..., Awaitable[Claims]]:
    """Admit a caller holding ANY of the given permissions (S14).

    The job list is reachable both by a clinician with `asr.read` and by
    a tenant_admin with only `stats.read`; the handler branches on which,
    and serves a PHI-free projection for the latter. Put the primary
    permission first — a denial is reported against it.
    """

    async def dep(claims: Annotated[Claims, Depends(current_user)]) -> Claims:
        try:
            check_any(claims, options=options)
        except AuthzDeniedError as exc:
            await _emit_authz_denied(exc)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"deny: roles={list(claims.roles)} hold none of "
                    f"{[f'{a} on {t}' for a, t in options]}"
                ),
            ) from exc
        return claims

    return dep


async def _emit_authz_denied(exc: AuthzDeniedError) -> None:
    _authz_denied_counter.add(
        1,
        {"action": exc.action, "target_kind": exc.target_kind, "reason": exc.reason},
    )
    state = _state
    if state is None:
        return
    try:
        await state.audit_writer.write_event(
            tenant_id=exc.claims.tid,
            kind="authz.denied",
            actor_sub=exc.claims.sub,
            actor_role=(exc.claims.roles[0] if exc.claims.roles else None),
            target_kind=exc.target_kind,
            target_id=None,
            payload={
                "action": exc.action,
                "target_kind": exc.target_kind,
                "reason": exc.reason,
                "roles_seen": list(exc.claims.roles),
            },
            severity=Severity.SEC,
        )
    except Exception as audit_exc:
        logger.warning(
            "authz_denied.audit_write_failed",
            extra={"error": str(audit_exc), "error_class": type(audit_exc).__name__},
        )
