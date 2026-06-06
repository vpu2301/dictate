"""Module-level holders for the lifespan-built dependencies.

FastAPI dependency injection needs callable factories at route-registration
time, but our deps (``current_user``, DB pools, audit writer) are
async-constructed during the lifespan. We bridge with module globals that
lifespan fills, plus thin functions that routers wire via ``Depends(...)``.

Public surface:
- :func:`current_user`  — verifies the bearer; returns :class:`Claims`.
- :func:`requires`       — factory; returns a ``Depends``-shaped function
  that checks the perms matrix and emits an ``authz.denied`` audit on 403.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from opentelemetry import metrics

from audit import Severity
from auth import Action, AuthzDeniedError, Claims, TargetKind, check

from .main_deps import ServiceState

logger = logging.getLogger(__name__)

_meter = metrics.get_meter("mdx.auth.authz")
_authz_denied_counter = _meter.create_counter(
    "mdx_authz_denied_total",
    description="Times requires() rejected a caller",
    unit="1",
)

# Filled by lifespan; raise if a router resolves a dep before lifespan ran.
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
    """The auth dep, callable as a FastAPI Depends.

    Defers the actual verification to the libs/auth-built closure, which
    needs the JwksCache from ServiceState. By making this a thin wrapper
    we keep the FastAPI signature stable for OpenAPI introspection.
    """
    state = get_state()
    if not hasattr(state, "_current_user_dep"):
        from auth import build_current_user

        from .config import settings

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
    """Return a FastAPI dependency that enforces the perms matrix.

    Usage::

        @router.get("/things")
        async def list_things(
            claims: Annotated[Claims, Depends(requires("thing.read", "thing"))],
        ): ...

    On deny: raises HTTPException(403) with an RFC 9457 detail; emits an
    ``authz.denied`` audit event (severity ``sec``) with the caller's tid+sub
    plus the attempted action/target. Audit emission is best-effort —
    failure to write the audit row never blocks the 403 response.
    """

    async def dep(claims: Annotated[Claims, Depends(current_user)]) -> Claims:
        try:
            check(claims, action=action, target_kind=target_kind, scope=scope)
        except AuthzDeniedError as exc:
            await _emit_authz_denied(exc)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"deny: roles={list(claims.roles)} cannot {action!r} "
                    f"on {target_kind!r}"
                    + (
                        f"; required scope {exc.required_scope!r}"
                        if exc.reason == "scope_missing"
                        else ""
                    )
                ),
            ) from exc
        return claims

    return dep


def requires_mfa() -> Callable[..., Awaitable[Claims]]:
    """Feature-flagged MFA gate.

    Sprint 02 ships with MFA off — see ``MDX_REQUIRE_MFA``. When the flag
    is **off**, this dep is a no-op (returns whatever ``current_user``
    resolves). When **on**, it asserts ``claims.mfa is True`` and otherwise
    responds 401 with ``WWW-Authenticate: MFA``, signalling to the frontend
    that the user must satisfy a TOTP challenge.

    Flag is read on every call (not at module import) so an operator can
    flip ``MDX_REQUIRE_MFA`` and bounce the service without code changes,
    and so tests can monkeypatch the setting per-case.

    The matching client-side flow (TOTP enrolment + redemption endpoints)
    is intentionally not built in sprint 02 — that's sprint 16+.
    """
    from .config import settings

    async def dep(
        claims: Annotated[Claims, Depends(current_user)],
    ) -> Claims:
        if settings.require_mfa and not claims.mfa:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MFA required for this endpoint",
                headers={"WWW-Authenticate": 'MFA realm="medical-dictation"'},
            )
        return claims

    return dep


async def _emit_authz_denied(exc: AuthzDeniedError) -> None:
    """Write an ``authz.denied`` sec event. Never raise — the caller is
    already responding 403; a failed audit write must not become a 500."""
    _authz_denied_counter.add(
        1,
        {
            "action": exc.action,
            "target_kind": exc.target_kind,
            "reason": exc.reason,
        },
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
                "required_scope": exc.required_scope,
                "roles_seen": list(exc.claims.roles),
            },
            severity=Severity.SEC,
        )
    except Exception as audit_exc:
        logger.warning(
            "authz_denied.audit_write_failed",
            extra={
                "action": exc.action,
                "target_kind": exc.target_kind,
                "error": str(audit_exc),
                "error_class": type(audit_exc).__name__,
            },
        )


