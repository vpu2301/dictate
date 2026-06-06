"""FastAPI dependency: 401 + WWW-Authenticate on every failure mode.

NOTE: deliberately *no* ``from __future__ import annotations`` here. With
PEP 563 deferred evaluation, FastAPI cannot resolve ``Depends(current_user)``
when ``current_user`` is a fixture-local closure — ``typing.get_type_hints``
runs at introspection time in module scope and can't see the local.
"""

from collections.abc import Callable
from typing import Annotated

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from auth.claims import Claims
from auth.context import current_claims, current_tenant_id
from auth.dependencies import build_current_user
from auth.jwks import JwksCache

from ..conftest import AUDIENCE, ISSUER, TENANT_A

pytestmark = pytest.mark.asyncio


@pytest.fixture
def app(jwks_cache: JwksCache) -> FastAPI:
    current_user = build_current_user(
        jwks_cache=jwks_cache,
        expected_audience=AUDIENCE,
        expected_issuer=ISSUER,
    )

    fastapi_app = FastAPI()

    @fastapi_app.get("/protected")
    async def protected(claims: Annotated[Claims, Depends(current_user)]) -> dict[str, str]:
        # Confirm ContextVar wiring works end-to-end.
        assert current_claims() is not None
        assert current_tenant_id() == TENANT_A
        return {"sub": str(claims.sub), "tid": str(claims.tid)}

    return fastapi_app


@pytest_asyncio.fixture
async def client(app: FastAPI):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_valid_token_grants_access(
    client: AsyncClient, mint_token: Callable[..., str]
) -> None:
    token = mint_token()
    resp = await client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        # surface FastAPI's validation reason for debugging
        print("DEBUG body:", resp.text)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tid"] == str(TENANT_A)


async def test_missing_header_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/protected")
    assert resp.status_code == 401
    assert "Bearer" in resp.headers.get("WWW-Authenticate", "")


async def test_wrong_scheme_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/protected", headers={"Authorization": "Basic abc=="})
    assert resp.status_code == 401


async def test_garbage_token_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/protected", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


async def test_expired_token_returns_401(
    client: AsyncClient, mint_token: Callable[..., str]
) -> None:
    import time

    now = int(time.time())
    token = mint_token(payload_overrides={"iat": now - 3600, "exp": now - 60})
    resp = await client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "expired" in resp.text.lower()
