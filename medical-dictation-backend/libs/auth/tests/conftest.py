"""Test fixtures: RSA keypair, JWKS doc, token minter, MockTransport JWKS server."""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt as jose_jwt

from auth.jwks import JwksCache

ISSUER = "https://issuer.test/realms/medical-dictation"
JWKS_URL = "https://issuer.test/realms/medical-dictation/protocol/openid-connect/certs"
AUDIENCE = "mdx-api"
TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
SUB_A = UUID("11111111-1111-1111-1111-111111111111")


def _b64url_uint(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass
class RSATestKey:
    kid: str
    private_pem: bytes
    public_jwk: dict[str, str]


def _generate_rsa_key(kid: str) -> RSATestKey:
    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = pk.public_key().public_numbers()
    private_pem = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    jwk = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64url_uint(nums.n),
        "e": _b64url_uint(nums.e),
    }
    return RSATestKey(kid=kid, private_pem=private_pem, public_jwk=jwk)


@pytest.fixture(scope="session")
def rsa_key_1() -> RSATestKey:
    return _generate_rsa_key("test-key-1")


@pytest.fixture(scope="session")
def rsa_key_2() -> RSATestKey:
    """Second key for rotation tests."""
    return _generate_rsa_key("test-key-2")


@pytest.fixture(scope="session")
def rsa_key_unknown() -> RSATestKey:
    """A key the JWKS server will *not* publish — used to mint forged tokens."""
    return _generate_rsa_key("never-published")


def _mint(
    *,
    private_pem: bytes,
    kid: str,
    alg: str = "RS256",
    payload: dict[str, Any] | None = None,
) -> str:
    headers = {"kid": kid, "alg": alg}
    default_payload = _valid_payload()
    if payload:
        merged = {**default_payload, **payload}
    else:
        merged = default_payload
    return jose_jwt.encode(merged, private_pem, algorithm=alg, headers=headers)


def _valid_payload() -> dict[str, Any]:
    now = int(time.time())
    return {
        "sub": str(SUB_A),
        "tid": str(TENANT_A),
        "roles": ["clinician"],
        "scope": "openid email profile",
        "sid": "session-1",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 900,
    }


@pytest.fixture
def mint_token(rsa_key_1: RSATestKey) -> Callable[..., str]:
    """Mint a JWT signed by ``rsa_key_1``. Override payload fields via kwargs."""

    def _mint_default(
        *,
        kid: str | None = None,
        alg: str = "RS256",
        payload_overrides: dict[str, Any] | None = None,
        private_pem: bytes | None = None,
    ) -> str:
        return _mint(
            private_pem=private_pem or rsa_key_1.private_pem,
            kid=kid or rsa_key_1.kid,
            alg=alg,
            payload=payload_overrides,
        )

    return _mint_default


@dataclass
class FakeJwksServer:
    """In-memory JWKS endpoint exposed via httpx.MockTransport."""

    keys: list[dict[str, Any]]
    call_count: int = 0
    extra_handler: Callable[[httpx.Request], httpx.Response | None] | None = None
    delay_seconds: float = 0.0
    pending_releases: list[Any] = field(default_factory=list)

    def jwks(self) -> dict[str, Any]:
        return {"keys": self.keys}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.extra_handler is not None:
            override = self.extra_handler(request)
            if override is not None:
                return override
        self.call_count += 1
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json=self.jwks())
        return httpx.Response(404, json={"error": "not found"})

    def add_key(self, jwk: dict[str, Any]) -> None:
        self.keys.append(jwk)

    def replace_keys(self, jwks: list[dict[str, Any]]) -> None:
        self.keys = jwks


@pytest_asyncio.fixture
async def jwks_server(rsa_key_1: RSATestKey) -> FakeJwksServer:
    return FakeJwksServer(keys=[rsa_key_1.public_jwk])


@pytest_asyncio.fixture
async def jwks_cache(jwks_server: FakeJwksServer):
    transport = httpx.MockTransport(jwks_server.handler)
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    cache = JwksCache(
        issuer_to_url={ISSUER: JWKS_URL},
        ttl_seconds=300,
        refresh_rate_limit_seconds=5,
        http_client=client,
    )
    try:
        yield cache
    finally:
        await cache.aclose()
