"""libs/auth — JWT verification, JWKS caching, FastAPI integration.

Public API (everything else is implementation detail and may change):

- :class:`Claims` — strict pydantic model of the verified token payload.
- :func:`verify_token` — the single sanctioned verification entry point.
- :class:`JwksCache` — async JWKS cache with TTL and storm prevention.
- :func:`build_current_user` — factory for the FastAPI dependency.
- :func:`current_claims`, :func:`current_tenant_id` — per-request ContextVar
  accessors for code that runs outside an explicit ``Depends`` injection.
- The :mod:`auth.exceptions` module re-exports every distinct failure type.

ADR-0006 covers the design rationale (Keycloak as IdP, RS256-only, strict
claims model).
"""

from __future__ import annotations

from .claims import Claims
from .context import (
    current_claims,
    current_tenant_id,
    require_current_claims,
    reset_current_claims,
    set_current_claims,
)
from .dependencies import build_current_user, requires_mfa
from .exceptions import (
    AuthError,
    ExpiredTokenError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidTokenError,
    JwksFetchError,
    KidNotFoundError,
    MalformedClaimsError,
)
from .jwks import JwksCache, JwksMetrics
from .perms import (
    ALLOW,
    KNOWN_ROLES,
    KNOWN_TARGET_KINDS,
    Action,
    AuthzDeniedError,
    Role,
    TargetKind,
    can,
    check,
)
from .verifier import verify_token

__all__ = [
    "ALLOW",
    "Action",
    "AuthError",
    "AuthzDeniedError",
    "Claims",
    "ExpiredTokenError",
    "InvalidAudienceError",
    "InvalidIssuerError",
    "InvalidTokenError",
    "JwksCache",
    "JwksFetchError",
    "JwksMetrics",
    "KNOWN_ROLES",
    "KNOWN_TARGET_KINDS",
    "KidNotFoundError",
    "MalformedClaimsError",
    "Role",
    "TargetKind",
    "build_current_user",
    "can",
    "check",
    "current_claims",
    "current_tenant_id",
    "require_current_claims",
    "requires_mfa",
    "reset_current_claims",
    "set_current_claims",
    "verify_token",
]
