"""FastAPI dependencies that turn libs/auth into a one-liner for services.

Usage in a service::

    from auth.dependencies import build_current_user
    from auth.jwks import JwksCache

    jwks_cache = JwksCache(issuer_to_url={settings.issuer: settings.jwks_url})
    current_user = build_current_user(
        jwks_cache=jwks_cache,
        expected_audience=settings.expected_audience,
        expected_issuer=settings.issuer,
    )

    @router.get("/me")
    async def me(claims: Annotated[Claims, Depends(current_user)]) -> ...:
        ...

The dependency raises ``HTTPException(401)`` with a ``WWW-Authenticate:
Bearer`` challenge on any verification failure. The observability lib's
problem-details handler renders it as RFC 9457 ``application/problem+json``.
"""

from __future__ import annotations

from typing import Annotated, Callable, Coroutine

from fastapi import Header, HTTPException, Request, status

from .claims import Claims
from .context import set_current_claims
from .exceptions import (
    AuthError,
    ExpiredTokenError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidTokenError,
    KidNotFoundError,
    MalformedClaimsError,
)
from .jwks import JwksCache
from .verifier import verify_token


_WWW_AUTHENTICATE = "Bearer realm=\"medical-dictation\""


def _unauthorized(detail: str, *, extra_challenge: str | None = None) -> HTTPException:
    challenge = _WWW_AUTHENTICATE
    if extra_challenge:
        challenge = f"{challenge}, {extra_challenge}"
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": challenge},
    )


def build_current_user(
    *,
    jwks_cache: JwksCache,
    expected_audience: str,
    expected_issuer: str,
    clock_skew_seconds: int = 30,
) -> Callable[..., Coroutine[None, None, Claims]]:
    """Return a FastAPI dependency that yields verified :class:`Claims`.

    The returned coroutine reads the ``Authorization: Bearer …`` header,
    verifies the token, sets the per-request claims ContextVar, stashes
    the claims on ``request.state.claims`` for non-Depends consumers, and
    returns the :class:`Claims` instance.
    """

    async def _current_user(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Claims:
        if authorization is None:
            raise _unauthorized("Authorization header is required")
        if not authorization.startswith("Bearer "):
            raise _unauthorized("Authorization header must use the Bearer scheme")
        token = authorization[len("Bearer ") :].strip()
        if not token:
            raise _unauthorized("Bearer token is empty")

        try:
            claims = await verify_token(
                token,
                expected_audience=expected_audience,
                expected_issuer=expected_issuer,
                jwks_cache=jwks_cache,
                clock_skew_seconds=clock_skew_seconds,
            )
        except ExpiredTokenError as exc:
            raise _unauthorized(f"Token expired: {exc}") from exc
        except InvalidAudienceError as exc:
            raise _unauthorized(f"Invalid audience: {exc}") from exc
        except InvalidIssuerError as exc:
            raise _unauthorized(f"Invalid issuer: {exc}") from exc
        except KidNotFoundError as exc:
            raise _unauthorized(f"Signing key not recognised: {exc}") from exc
        except MalformedClaimsError as exc:
            raise _unauthorized(f"Token claims malformed: {exc}") from exc
        except InvalidTokenError as exc:
            raise _unauthorized(f"Invalid token: {exc}") from exc
        except AuthError as exc:
            # Catch-all for any future AuthError subclass we add later.
            raise _unauthorized(str(exc)) from exc

        set_current_claims(claims)
        request.state.claims = claims
        return claims

    return _current_user


def requires_mfa(claims: Claims) -> Claims:
    """Helper used by routes that must reject non-MFA tokens.

    Sprint 02 ships with MFA disabled by feature flag; the actual flag
    check lives in :mod:`auth.feature_flags` (Day 8). This raw helper is
    safe to call regardless — it just enforces the rule whenever it is
    used. Callers that want feature-flag awareness should use
    ``build_requires_mfa`` from Day 8 instead.
    """
    if not claims.mfa:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MFA required for this endpoint",
            headers={"WWW-Authenticate": "MFA realm=\"medical-dictation\""},
        )
    return claims
