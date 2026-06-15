"""Per-request ContextVar carrying the verified :class:`Claims`.

A FastAPI dependency that calls :func:`set_current_claims` makes the claims
available to any code further down the call chain in the same async Task
— without threading them explicitly through every function argument.

The intended consumer is ``libs/db.tenant_connection``: services can write
``async with tenant_connection(pool, current_tenant_id()): ...`` and let the
ContextVar handle the wiring. The explicit-argument form remains available
for service code that prefers it.

ContextVar.set creates a per-Task binding; FastAPI runs each request in a
fresh asyncio Task, so the binding is naturally request-scoped. Tests can
override via :func:`reset_current_claims`.
"""

from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID

from .claims import Claims

_current_claims: ContextVar[Claims | None] = ContextVar("_current_claims", default=None)


def set_current_claims(claims: Claims) -> None:
    """Bind ``claims`` to the current async Task / request."""
    _current_claims.set(claims)


def reset_current_claims() -> None:
    """Clear the ContextVar (test helper / explicit logout flows)."""
    _current_claims.set(None)


def current_claims() -> Claims | None:
    """Return the claims bound to this request, or ``None`` outside a request."""
    return _current_claims.get()


def current_tenant_id() -> UUID | None:
    """Convenience: return the ``tid`` from the bound claims, if any."""
    claims = _current_claims.get()
    return claims.tid if claims is not None else None


def require_current_claims() -> Claims:
    """Return claims or raise ``RuntimeError`` — for code that must have them.

    Distinct from FastAPI's dependency injection: this is for non-handler
    code paths (background tasks, library internals) that should never have
    been reached without an authenticated context.
    """
    claims = _current_claims.get()
    if claims is None:
        raise RuntimeError(
            "current_claims is unset; this code path must run inside an authenticated request scope"
        )
    return claims
