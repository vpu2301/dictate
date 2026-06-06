"""``verify_token`` — the single sanctioned entry point for JWT verification.

The function is intentionally narrow: it accepts a raw token string and the
expected issuer/audience, then either returns a :class:`Claims` or raises
one of the distinct exception classes from :mod:`auth.exceptions`. Every
failure mode is mapped to its own type so callers can audit/alert
appropriately (e.g. ``InvalidTokenError`` is a sec-severity event).

This module is named ``verifier`` rather than ``jwt`` to avoid shadowing
the ``jose.jwt`` import inside our own package.
"""

from __future__ import annotations

from typing import Any

from jose import ExpiredSignatureError, JWTError, jwk, jwt
from jose.exceptions import JWTClaimsError
from pydantic import ValidationError

from .claims import Claims
from .exceptions import (
    ExpiredTokenError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidTokenError,
    KidNotFoundError,
    MalformedClaimsError,
)
from .jwks import JwksCache

_ACCEPTED_ALGORITHMS: frozenset[str] = frozenset({"RS256"})


async def verify_token(
    token: str,
    *,
    expected_audience: str,
    expected_issuer: str,
    jwks_cache: JwksCache,
    clock_skew_seconds: int = 30,
) -> Claims:
    """Verify ``token`` and return parsed :class:`Claims`.

    Raises:
        InvalidTokenError: token is structurally malformed, signature
            does not verify, or the header algorithm is not RS256.
        ExpiredTokenError: ``exp`` is in the past (after applying
            ``clock_skew_seconds`` of leeway).
        InvalidIssuerError: ``iss`` claim does not match ``expected_issuer``.
        InvalidAudienceError: ``aud`` does not include ``expected_audience``.
        KidNotFoundError: header ``kid`` is missing or not in JWKS.
        MalformedClaimsError: claims payload violates the Claims schema
            (missing mandatory field, unexpected field, wrong type).
    """
    # Parse header without verifying signature, then assert alg upfront so
    # we never let python-jose pick an alg for us (algorithm-confusion CVE class).
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise InvalidTokenError(f"malformed token header: {exc}") from exc

    alg = header.get("alg")
    if alg not in _ACCEPTED_ALGORITHMS:
        raise InvalidTokenError(
            f"unsupported alg {alg!r}; only RS256 is accepted"
        )

    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise KidNotFoundError("token header has no kid")

    jwk_dict = await jwks_cache.get_key(expected_issuer, kid)
    try:
        signing_key = jwk.construct(jwk_dict, algorithm="RS256")
    except Exception as exc:
        raise InvalidTokenError(f"could not construct verifier from JWK: {exc}") from exc

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=list(_ACCEPTED_ALGORITHMS),
            audience=expected_audience,
            issuer=expected_issuer,
            options={
                "leeway": clock_skew_seconds,
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": False,  # iat tolerated; exp is the auth horizon
            },
        )
    except ExpiredSignatureError as exc:
        raise ExpiredTokenError(str(exc)) from exc
    except JWTClaimsError as exc:
        # python-jose lumps aud/iss errors under JWTClaimsError; discriminate
        # by message so we emit the right audit kind.
        msg = str(exc).lower()
        if "audience" in msg or "aud " in msg:
            raise InvalidAudienceError(str(exc)) from exc
        if "issuer" in msg or "iss " in msg:
            raise InvalidIssuerError(str(exc)) from exc
        raise InvalidTokenError(str(exc)) from exc
    except JWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    try:
        return Claims(**payload)
    except ValidationError as exc:
        raise MalformedClaimsError(str(exc)) from exc
