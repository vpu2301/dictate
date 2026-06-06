"""Integration test against a live Keycloak instance.

Skipped unless ``RUN_KEYCLOAK_INTEGRATION=1``. Expects the Sprint-02 dev
realm to be running at ``http://localhost:8088``.

Run::

    make dev-up
    RUN_KEYCLOAK_INTEGRATION=1 uv run pytest libs/auth/tests/integration -v
"""

import os
from urllib.parse import urljoin

import httpx
import pytest
import pytest_asyncio

from auth import JwksCache, verify_token
from auth.claims import Claims

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("RUN_KEYCLOAK_INTEGRATION") != "1",
        reason="set RUN_KEYCLOAK_INTEGRATION=1 to run; needs a live Keycloak",
    ),
]

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://localhost:8088")
REALM = "medical-dictation"
CLIENT_ID = "mdx-dev-cli"
AUDIENCE = "mdx-api"
ISSUER = f"{KEYCLOAK_URL}/realms/{REALM}"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"


async def _password_grant(username: str, password: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            urljoin(KEYCLOAK_URL + "/", f"realms/{REALM}/protocol/openid-connect/token"),
            data={
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "username": username,
                "password": password,
                "scope": "openid",
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


@pytest_asyncio.fixture
async def jwks_cache():
    cache = JwksCache(issuer_to_url={ISSUER: JWKS_URL})
    try:
        yield cache
    finally:
        await cache.aclose()


async def test_real_token_verifies(jwks_cache: JwksCache) -> None:
    token = await _password_grant("dev-clinician", "dev-password")
    claims = await verify_token(
        token,
        expected_audience=AUDIENCE,
        expected_issuer=ISSUER,
        jwks_cache=jwks_cache,
    )
    assert isinstance(claims, Claims)
    assert "clinician" in claims.roles
    # Tenant A: 00...00a
    assert str(claims.tid).endswith("000a")


async def test_real_token_for_tenant_b(jwks_cache: JwksCache) -> None:
    token = await _password_grant("dev-clinician-b", "dev-password")
    claims = await verify_token(
        token,
        expected_audience=AUDIENCE,
        expected_issuer=ISSUER,
        jwks_cache=jwks_cache,
    )
    assert str(claims.tid).endswith("000b")
