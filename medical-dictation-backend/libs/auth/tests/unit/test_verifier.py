"""verify_token: every failure mode mapped to its distinct exception type."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from auth.claims import Claims
from auth.exceptions import (
    ExpiredTokenError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidTokenError,
    KidNotFoundError,
    MalformedClaimsError,
)
from auth.jwks import JwksCache
from auth.verifier import verify_token

from ..conftest import AUDIENCE, ISSUER, RSATestKey

pytestmark = pytest.mark.asyncio


async def test_valid_token_returns_claims(
    mint_token: Callable[..., str], jwks_cache: JwksCache
) -> None:
    token = mint_token()
    claims = await verify_token(
        token,
        expected_audience=AUDIENCE,
        expected_issuer=ISSUER,
        jwks_cache=jwks_cache,
    )
    assert isinstance(claims, Claims)
    assert claims.roles == ["clinician"]
    assert claims.mfa is False  # absence ⇒ default false


async def test_expired_token_raises_expired_token_error(
    mint_token: Callable[..., str], jwks_cache: JwksCache
) -> None:
    now = int(time.time())
    token = mint_token(payload_overrides={"iat": now - 3600, "exp": now - 31})
    with pytest.raises(ExpiredTokenError):
        await verify_token(
            token,
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )


async def test_token_within_clock_skew_accepted(
    mint_token: Callable[..., str], jwks_cache: JwksCache
) -> None:
    now = int(time.time())
    # 29 seconds past expiry: inside the default 30 s leeway → accepted.
    token = mint_token(payload_overrides={"iat": now - 3600, "exp": now - 29})
    claims = await verify_token(
        token,
        expected_audience=AUDIENCE,
        expected_issuer=ISSUER,
        jwks_cache=jwks_cache,
    )
    assert isinstance(claims, Claims)


async def test_token_just_past_clock_skew_rejected(
    mint_token: Callable[..., str], jwks_cache: JwksCache
) -> None:
    now = int(time.time())
    token = mint_token(payload_overrides={"iat": now - 3600, "exp": now - 31})
    with pytest.raises(ExpiredTokenError):
        await verify_token(
            token,
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )


async def test_wrong_issuer_raises_invalid_issuer_error(
    mint_token: Callable[..., str], jwks_cache: JwksCache
) -> None:
    token = mint_token(payload_overrides={"iss": "https://attacker.example/realm"})
    with pytest.raises(InvalidIssuerError):
        await verify_token(
            token,
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )


async def test_wrong_audience_raises_invalid_audience_error(
    mint_token: Callable[..., str], jwks_cache: JwksCache
) -> None:
    token = mint_token(payload_overrides={"aud": "wrong-audience"})
    with pytest.raises(InvalidAudienceError):
        await verify_token(
            token,
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )


async def test_missing_kid_raises_kid_not_found(
    rsa_key_1: RSATestKey, jwks_cache: JwksCache
) -> None:
    from jose import jwt as jose_jwt

    now = int(time.time())
    payload = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "tid": "00000000-0000-0000-0000-00000000000a",
        "roles": ["clinician"],
        "sid": "s",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 900,
    }
    # No kid in header.
    token = jose_jwt.encode(payload, rsa_key_1.private_pem, algorithm="RS256", headers={})
    with pytest.raises(KidNotFoundError):
        await verify_token(
            token,
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )


async def test_alg_none_fails_closed(jwks_cache: JwksCache, rsa_key_1: RSATestKey) -> None:
    """Algorithm-confusion class attack: token signed with alg=none must be rejected.

    python-jose deliberately refuses to *encode* an ``alg=none`` JWT, so we
    hand-craft the token bytes to simulate what an attacker would send.
    """
    import base64
    import json

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = {"alg": "none", "kid": rsa_key_1.kid, "typ": "JWT"}
    payload = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "tid": "00000000-0000-0000-0000-00000000000a",
        "roles": [],
        "sid": "s",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": 0,
        "exp": 9999999999,
    }
    head_b64 = b64url(json.dumps(header, separators=(",", ":")).encode())
    pay_b64 = b64url(json.dumps(payload, separators=(",", ":")).encode())
    token = f"{head_b64}.{pay_b64}."  # empty signature

    with pytest.raises(InvalidTokenError, match="unsupported alg"):
        await verify_token(
            token,
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )


async def test_alg_hs256_fails_closed(jwks_cache: JwksCache, rsa_key_1: RSATestKey) -> None:
    """HS256 with a guessed shared-secret must be rejected: only RS256 is accepted."""
    from jose import jwt as jose_jwt

    payload = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "tid": "00000000-0000-0000-0000-00000000000a",
        "roles": [],
        "sid": "s",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": 0,
        "exp": 9999999999,
    }
    token = jose_jwt.encode(
        payload, key="guessed-shared-secret", algorithm="HS256", headers={"kid": rsa_key_1.kid}
    )
    with pytest.raises(InvalidTokenError, match="unsupported alg"):
        await verify_token(
            token,
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )


async def test_extra_claim_is_admin_rejected(
    mint_token: Callable[..., str], jwks_cache: JwksCache
) -> None:
    """An attacker-injected extra claim must produce MalformedClaimsError."""
    token = mint_token(payload_overrides={"is_admin": True})
    with pytest.raises(MalformedClaimsError):
        await verify_token(
            token,
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )


async def test_missing_tid_raises_malformed_claims(
    rsa_key_1: RSATestKey, jwks_cache: JwksCache
) -> None:
    from jose import jwt as jose_jwt

    now = int(time.time())
    payload = {
        "sub": "11111111-1111-1111-1111-111111111111",
        # tid intentionally missing
        "roles": ["clinician"],
        "sid": "s",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 900,
    }
    token = jose_jwt.encode(
        payload, rsa_key_1.private_pem, algorithm="RS256", headers={"kid": rsa_key_1.kid}
    )
    with pytest.raises(MalformedClaimsError):
        await verify_token(
            token,
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )


async def test_forged_token_with_unknown_kid_rejected(
    rsa_key_unknown: RSATestKey, jwks_cache: JwksCache
) -> None:
    """Token signed by a key the IdP never published must produce KidNotFoundError."""
    from jose import jwt as jose_jwt

    now = int(time.time())
    payload = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "tid": "00000000-0000-0000-0000-00000000000a",
        "roles": ["tenant_admin"],
        "sid": "s",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 900,
    }
    token = jose_jwt.encode(
        payload,
        rsa_key_unknown.private_pem,
        algorithm="RS256",
        headers={"kid": rsa_key_unknown.kid},
    )
    with pytest.raises(KidNotFoundError):
        await verify_token(
            token,
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )


async def test_truncated_signature_rejected(
    mint_token: Callable[..., str], jwks_cache: JwksCache
) -> None:
    """Bit-flipping the signature must cause verification to fail closed."""
    token = mint_token()
    # Drop last 16 bytes of base64-url signature.
    head, sig = token.rsplit(".", 1)
    mangled = head + "." + sig[: max(len(sig) - 16, 0)]
    with pytest.raises(InvalidTokenError):
        await verify_token(
            mangled,
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )


async def test_malformed_header_rejected(jwks_cache: JwksCache) -> None:
    with pytest.raises(InvalidTokenError):
        await verify_token(
            "not.a.jwt",
            expected_audience=AUDIENCE,
            expected_issuer=ISSUER,
            jwks_cache=jwks_cache,
        )
