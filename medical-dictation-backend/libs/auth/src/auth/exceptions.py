"""Distinct exception classes for every JWT verification failure mode.

Distinct types matter because callers (FastAPI dependency, audit emitter,
operators reading logs) need to discriminate between, e.g., an expired token
(common, expected) and a malformed-claims token (rare, suspicious). Lumping
them under one ``AuthError`` discards the signal.
"""

from __future__ import annotations


class AuthError(Exception):
    """Base class for every libs/auth verification failure."""


class InvalidTokenError(AuthError):
    """Token is structurally invalid, signature mismatched, or algorithm not RS256."""


class ExpiredTokenError(AuthError):
    """Token's ``exp`` claim is in the past (accounting for clock skew)."""


class InvalidIssuerError(AuthError):
    """Token's ``iss`` claim does not match the expected issuer."""


class InvalidAudienceError(AuthError):
    """Token's ``aud`` claim does not contain the expected audience."""


class KidNotFoundError(AuthError):
    """The signing key id (``kid``) from the token header is not in JWKS.

    Raised both when JWKS has been fetched recently and the kid is missing
    (likely a forged token), and when the rate-limit prevents another fetch.
    """


class MalformedClaimsError(AuthError):
    """Token decoded successfully but the claims payload violates the Claims schema.

    Examples: missing mandatory ``tid``, presence of an unexpected claim
    (``extra="forbid"`` on the model), wrong types.
    """


class JwksFetchError(AuthError):
    """Could not retrieve the JWKS document from the IdP (network / 5xx / parse)."""
