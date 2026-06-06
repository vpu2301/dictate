"""End-to-end auth-service tests against the live dev stack.

Requires:
  * Keycloak running on http://localhost:8088 with the sprint-02 realm.
  * Postgres with migrations applied (``make migrate-up``).

Run with both env flags set:
  ``RUN_DB_INTEGRATION=1 RUN_KEYCLOAK_INTEGRATION=1 pytest``
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1"
    or os.environ.get("RUN_KEYCLOAK_INTEGRATION") != "1",
    reason="needs RUN_DB_INTEGRATION=1 + RUN_KEYCLOAK_INTEGRATION=1 + the dev stack",
)

# Force testing mode before importing auth_service (disables OTel).
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from auth_service.main import create_app  # noqa: E402

KEYCLOAK = "http://localhost:8088"
REALM = "medical-dictation"
TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
SU_DSN = "postgresql://postgres:postgres@localhost:5432/medical_dictation"


@pytest_asyncio.fixture
async def app():
    a = create_app()
    # Drive the FastAPI lifespan manually (no uvicorn).
    async with httpx.AsyncClient(transport=ASGITransport(app=a), base_url="http://test") as _:
        async with a.router.lifespan_context(a):
            yield a


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def superuser_conn():
    conn = await asyncpg.connect(SU_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(autouse=True)
async def _wipe_state(superuser_conn: asyncpg.Connection):
    # Each test starts clean — wipe both DB and Keycloak side. The `email=`
    # query param does a substring match (Keycloak's `search=` doesn't span
    # the `@`, so we don't use it here).
    async def _wipe_keycloak() -> None:
        try:
            admin = await _kc_admin_token()
            users = await _kc_admin_get(admin, "users?email=e2e.test&max=200")
            for u in users:
                await _kc_admin_delete(admin, f"users/{u['id']}")
        except Exception:
            pass

    await superuser_conn.execute("TRUNCATE audit.events")
    await superuser_conn.execute("DELETE FROM users WHERE email LIKE '%@e2e.test'")
    await _wipe_keycloak()
    yield
    await superuser_conn.execute("DELETE FROM users WHERE email LIKE '%@e2e.test'")
    await _wipe_keycloak()


# ── helpers ──────────────────────────────────────────────────────────────


async def _kc_admin_token() -> str:
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "mdx-admin",
                "client_secret": "dev-secret-change-in-prod-mdx-admin",
            },
        )
        r.raise_for_status()
        return r.json()["access_token"]  # type: ignore[no-any-return]


async def _kc_admin_get(token: str, path: str):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(
            f"{KEYCLOAK}/admin/realms/{REALM}/{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        return r.json()


async def _kc_admin_delete(token: str, path: str):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.delete(
            f"{KEYCLOAK}/admin/realms/{REALM}/{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()


# ── /auth/login ─────────────────────────────────────────────────────────


async def test_login_happy_path(client: AsyncClient, superuser_conn: asyncpg.Connection):
    r = await client.post(
        "/auth/login", json={"username": "dev-clinician", "password": "dev-password"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "access_token" in body
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] > 0
    # Refresh cookie was set on /auth path, HttpOnly.
    cookies = r.headers.get_list("set-cookie")
    assert any("mdx_rt=" in c for c in cookies)
    assert any("HttpOnly" in c for c in cookies)

    # Audit row created for tenant A.
    n = await superuser_conn.fetchval(
        "SELECT count(*) FROM audit.events WHERE tenant_id=$1 AND kind='auth.login'",
        TENANT_A,
    )
    assert n == 1


async def test_login_invalid_password_returns_401(client: AsyncClient):
    r = await client.post(
        "/auth/login", json={"username": "dev-clinician", "password": "wrong"}
    )
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


async def test_login_unknown_user_returns_401(client: AsyncClient):
    r = await client.post(
        "/auth/login", json={"username": "ghost@nowhere.test", "password": "x"}
    )
    assert r.status_code == 401


# ── /auth/refresh ───────────────────────────────────────────────────────


async def test_refresh_rotates(client: AsyncClient):
    r1 = await client.post(
        "/auth/login", json={"username": "dev-clinician", "password": "dev-password"}
    )
    assert r1.status_code == 200
    old_cookie = client.cookies.get("mdx_rt")
    assert old_cookie

    r2 = await client.post("/auth/refresh")
    assert r2.status_code == 200, r2.text
    new_cookie = client.cookies.get("mdx_rt")
    assert new_cookie
    assert new_cookie != old_cookie


async def test_refresh_without_cookie_401(client: AsyncClient):
    r = await client.post("/auth/refresh")
    assert r.status_code == 401


# ── /auth/logout ────────────────────────────────────────────────────────


async def test_logout_clears_cookie(client: AsyncClient, superuser_conn):
    login = await client.post(
        "/auth/login", json={"username": "dev-clinician", "password": "dev-password"}
    )
    access = login.json()["access_token"]
    # Logout with Authorization header so audit context (tid+sub) is known.
    r = await client.post("/auth/logout", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 204
    # Cookie should be cleared (delete_cookie sets a past expiry).
    sc = r.headers.get("set-cookie", "")
    assert "mdx_rt=" in sc

    n = await superuser_conn.fetchval(
        "SELECT count(*) FROM audit.events WHERE tenant_id=$1 AND kind='auth.logout'",
        TENANT_A,
    )
    assert n == 1


async def test_logout_without_access_token_still_clears_cookie(
    client: AsyncClient, superuser_conn
):
    """Logout works even without the access token — the refresh is still
    revoked. The auth.logout audit is skipped because tid is unrecoverable
    from a refresh token alone."""
    await client.post(
        "/auth/login", json={"username": "dev-clinician", "password": "dev-password"}
    )
    r = await client.post("/auth/logout")
    assert r.status_code == 204
    assert "mdx_rt=" in r.headers.get("set-cookie", "")


# ── /auth/me ────────────────────────────────────────────────────────────


async def test_me_with_valid_token(client: AsyncClient):
    r1 = await client.post(
        "/auth/login", json={"username": "dev-clinician", "password": "dev-password"}
    )
    token = r1.json()["access_token"]
    r2 = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["claims"]["tid"] == str(TENANT_A)
    assert "clinician" in body["claims"]["roles"]
    # No DB row exists for dev-clinician (Keycloak-only user); endpoint
    # surfaces this as db_user=None — correct behaviour for sprint 02.
    assert body["db_user"] is None


async def test_me_without_token_401(client: AsyncClient):
    r = await client.get("/auth/me")
    assert r.status_code == 401


# ── /admin/users/invite ────────────────────────────────────────────────


async def test_invite_requires_tenant_admin(client: AsyncClient):
    r1 = await client.post(
        "/auth/login", json={"username": "dev-clinician", "password": "dev-password"}
    )
    token = r1.json()["access_token"]
    r2 = await client.post(
        "/admin/users/invite",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "newbie@e2e.test", "display_name": "Newbie", "role": "clinician"},
    )
    assert r2.status_code == 403


async def test_invite_happy_path(client: AsyncClient, superuser_conn):
    r1 = await client.post(
        "/auth/login", json={"username": "dev-admin", "password": "dev-password"}
    )
    token = r1.json()["access_token"]
    r2 = await client.post(
        "/admin/users/invite",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "newbie@e2e.test", "display_name": "Newbie One", "role": "clinician"},
    )
    assert r2.status_code == 201, r2.text
    body = r2.json()
    assert body["email"] == "newbie@e2e.test"
    assert body["role"] == "clinician"
    assert body["status"] == "invited"

    # DB row exists under tenant A.
    row = await superuser_conn.fetchrow(
        "SELECT email, role, status, tenant_id FROM users WHERE email = 'newbie@e2e.test'"
    )
    assert row is not None
    assert row["tenant_id"] == TENANT_A
    assert row["role"] == "clinician"
    assert row["status"] == "invited"

    # Audit row.
    n = await superuser_conn.fetchval(
        "SELECT count(*) FROM audit.events WHERE tenant_id=$1 AND kind='user.invited'",
        TENANT_A,
    )
    assert n == 1


# ── /admin/users/{sub}/deactivate ─────────────────────────────────────


async def test_deactivate_unknown_user_404(client: AsyncClient):
    r1 = await client.post(
        "/auth/login", json={"username": "dev-admin", "password": "dev-password"}
    )
    token = r1.json()["access_token"]
    r2 = await client.post(
        f"/admin/users/{uuid.uuid4()}/deactivate",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 404


async def test_authz_denied_emits_audit_event(
    client: AsyncClient, superuser_conn: asyncpg.Connection
):
    """The ``requires()`` dep must emit an ``authz.denied`` audit row with
    severity ``sec`` whenever it rejects a caller with the wrong role."""
    # Log in as a clinician — has no auditor/tenant_admin permissions.
    r1 = await client.post(
        "/auth/login", json={"username": "dev-clinician", "password": "dev-password"}
    )
    token = r1.json()["access_token"]

    # Attempt audit.read (allowed only for auditor / tenant_admin).
    r2 = await client.get(
        "/audit/events", headers={"Authorization": f"Bearer {token}"}
    )
    assert r2.status_code == 403

    # Verify the audit row.
    row = await superuser_conn.fetchrow(
        """
        SELECT severity, payload_jcs::text AS payload, actor_role
        FROM audit.events
        WHERE tenant_id = $1 AND kind = 'authz.denied'
        ORDER BY seq DESC LIMIT 1
        """,
        TENANT_A,
    )
    assert row is not None, "no authz.denied audit row was written"
    assert row["severity"] == "sec"
    import json as _json

    payload = _json.loads(row["payload"])["payload"]
    assert payload["action"] == "audit.read"
    assert payload["target_kind"] == "audit"
    assert payload["reason"] == "role_denied"
    assert "clinician" in payload["roles_seen"]


async def test_mfa_off_admin_invite_succeeds(
    client: AsyncClient, superuser_conn: asyncpg.Connection
):
    """With MDX_REQUIRE_MFA=false (the sprint-02 default) the requires_mfa()
    dep is a no-op — admin invite works even though dev tokens have mfa=false."""
    from auth_service.config import settings

    assert settings.require_mfa is False, "default must be off in sprint 02"

    r1 = await client.post(
        "/auth/login", json={"username": "dev-admin", "password": "dev-password"}
    )
    token = r1.json()["access_token"]
    r2 = await client.post(
        "/admin/users/invite",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "mfa-off@e2e.test", "display_name": "MFA Off", "role": "clinician"},
    )
    assert r2.status_code == 201, r2.text


async def test_mfa_on_admin_invite_rejected_with_mfa_challenge(
    client: AsyncClient, monkeypatch
):
    """Flipping MDX_REQUIRE_MFA=true makes the requires_mfa() dep enforce.
    Dev tokens carry mfa=false (no TOTP enrolled) → expect HTTP 401 with
    WWW-Authenticate: MFA so the frontend can prompt for the OTP."""
    from auth_service.config import settings

    monkeypatch.setattr(settings, "require_mfa", True)

    r1 = await client.post(
        "/auth/login", json={"username": "dev-admin", "password": "dev-password"}
    )
    token = r1.json()["access_token"]
    r2 = await client.post(
        "/admin/users/invite",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "mfa-on@e2e.test", "display_name": "MFA On", "role": "clinician"},
    )
    assert r2.status_code == 401, r2.text
    assert "MFA" in r2.headers.get("WWW-Authenticate", "")


async def test_mfa_on_audit_routes_unaffected(client: AsyncClient, monkeypatch):
    """The MFA gate is wired only on /admin/* in sprint 02. /audit/* is
    role-gated but not MFA-gated — flipping the flag must not break audit
    reads for an auditor."""
    from auth_service.config import settings

    monkeypatch.setattr(settings, "require_mfa", True)

    r1 = await client.post(
        "/auth/login", json={"username": "dev-auditor", "password": "dev-password"}
    )
    token = r1.json()["access_token"]
    r2 = await client.get(
        "/audit/events", headers={"Authorization": f"Bearer {token}"}
    )
    assert r2.status_code == 200, r2.text


async def test_deactivate_happy_path(client: AsyncClient, superuser_conn):
    # Step 1: invite a user we can then deactivate.
    r1 = await client.post(
        "/auth/login", json={"username": "dev-admin", "password": "dev-password"}
    )
    token = r1.json()["access_token"]
    invite = await client.post(
        "/admin/users/invite",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "doomed@e2e.test", "display_name": "Doomed User", "role": "clinician"},
    )
    sub = invite.json()["sub"]

    # Step 2: deactivate.
    r2 = await client.post(
        f"/admin/users/{sub}/deactivate",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "deactivated"

    # Step 3: DB reflects.
    row = await superuser_conn.fetchrow(
        "SELECT status FROM users WHERE sub = $1", uuid.UUID(sub)
    )
    assert row["status"] == "deactivated"

    # Step 4: audit row (sec severity).
    sev = await superuser_conn.fetchval(
        "SELECT severity FROM audit.events WHERE tenant_id=$1 AND kind='user.deactivated'",
        TENANT_A,
    )
    assert sev == "sec"
