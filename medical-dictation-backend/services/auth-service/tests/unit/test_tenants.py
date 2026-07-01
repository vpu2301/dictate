"""Unit tests for the tenant (clinic) management surface.

The repository SQL is exercised against a real Postgres in the DB-integration
suite; here we stub ``tenant_connection`` + the repo functions and drive the
router to assert the authorization / isolation / validation behaviour:

* tenant create + update (happy path, audit, owner bootstrap)
* role-based access — a clinician token cannot create/update a tenant
* tenant isolation — you can't read a tenant you are not a member of
* writes are scoped to the caller's active tenant
* membership creation, last-owner guard, role validation
* unauthorized (no membership) access is blocked
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")
ADMIN_SUB = UUID("0a000000-0000-0000-0000-00000000000a")
NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _claims(*, roles: list[str], tid: UUID = TENANT_A, sub: UUID = ADMIN_SUB) -> Claims:
    return Claims(
        sub=sub,
        tid=tid,
        roles=roles,
        scope="",
        mfa=False,
        sid="s",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )


def _tenant_row(tenant_id: UUID = TENANT_A, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": tenant_id,
        "name": "tenant-a",
        "display_name": "Dev Hospital A",
        "legal_name": "Dev Hospital A LLC",
        "slug": "tenant-a",
        "locale": "uk",
        "timezone": "Europe/Kyiv",
        "status": "active",
        "is_active": True,
        "logo_url": "",
        "logo_content_type": "",
        "contact_email": "contact@tenant-a.example",
        "phone_number": "",
        "website": "",
        "address_line1": "",
        "address_line2": "",
        "postal_code": "",
        "city": "Kyiv",
        "state_or_region": "",
        "country": "Ukraine",
        "tax_id": "",
        "registration_number": "",
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(over)
    return base


def _membership_row(role: str = "owner", tenant_id: UUID = TENANT_A) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "user_sub": ADMIN_SUB,
        "role": role,
        "status": "active",
        "invited_by": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch):
    """Factory: build a TestClient whose current_user resolves to given claims,
    with tenant_connection + audit stubbed. Repo functions are patched per-test."""
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from auth_service import deps
    from auth_service.main import create_app
    from auth_service.routers import tenants

    audit_calls: list[dict] = []

    async def _write_event(**kwargs: Any) -> None:
        audit_calls.append(kwargs)

    deps.install_state(  # type: ignore[arg-type]
        SimpleNamespace(
            app_pool=object(),
            tenant_writer_pool=object(),
            audit_writer=SimpleNamespace(write_event=_write_event),
        )
    )

    @contextlib.asynccontextmanager
    async def _fake_conn(pool: Any, tenant_id: Any):
        yield SimpleNamespace()

    monkeypatch.setattr(tenants, "tenant_connection", _fake_conn)
    # requires_mfa() reads settings.require_mfa (off by default) — leave as-is.

    def _build(claims: Claims) -> TestClient:
        app = create_app()
        app.dependency_overrides[deps.current_user] = lambda: claims
        c = TestClient(app)
        c.audit_calls = audit_calls  # type: ignore[attr-defined]
        return c

    return _build


# ── Create ───────────────────────────────────────────────────────────────


def test_create_tenant_happy_path(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    from auth_service.routers import tenants

    created: dict[str, Any] = {}

    async def _create_tenant(conn, **kw):  # noqa: ANN001
        created.update(kw)
        return _tenant_row(uuid4(), name=kw["name"], slug=kw["slug"])

    async def _add_member(conn, **kw):  # noqa: ANN001
        created["owner_sub"] = kw["user_sub"]
        return _membership_row(kw["role"])

    monkeypatch.setattr(tenants.repo, "create_tenant", _create_tenant)
    monkeypatch.setattr(tenants.repo, "add_member", _add_member)

    client = make_client(_claims(roles=["tenant_admin"]))
    resp = client.post("/tenants", json={"name": "kyiv-clinic", "display_name": "Kyiv Clinic"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "kyiv-clinic"
    assert body["my_role"] == "owner"
    # Creator was made the founding owner.
    assert created["owner_sub"] == str(ADMIN_SUB) or created["owner_sub"] == ADMIN_SUB
    kinds = [c["kind"] for c in client.audit_calls]  # type: ignore[attr-defined]
    assert "tenant.created" in kinds


def test_create_tenant_forbidden_for_clinician(make_client) -> None:
    client = make_client(_claims(roles=["clinician"]))
    resp = client.post("/tenants", json={"name": "x-clinic", "display_name": "X"})
    assert resp.status_code == 403


def test_create_tenant_rejects_bad_slug(make_client) -> None:
    client = make_client(_claims(roles=["tenant_admin"]))
    resp = client.post(
        "/tenants",
        json={"name": "ok-name", "display_name": "Ok", "slug": "Bad Slug!"},
    )
    assert resp.status_code == 422


# ── Update ───────────────────────────────────────────────────────────────


def test_update_tenant_happy_path(make_client, monkeypatch: pytest.MonkeyPatch) -> None:
    from auth_service.routers import tenants

    async def _get_membership(conn, *, tenant_id, user_sub):  # noqa: ANN001
        return _membership_row("owner")

    async def _update_tenant(conn, *, tenant_id, fields):  # noqa: ANN001
        return _tenant_row(tenant_id, **{k: v for k, v in fields.items() if k in _tenant_row()})

    monkeypatch.setattr(tenants.repo, "get_membership", _get_membership)
    monkeypatch.setattr(tenants.repo, "update_tenant", _update_tenant)

    client = make_client(_claims(roles=["tenant_admin"]))
    resp = client.patch(f"/tenants/{TENANT_A}", json={"city": "Lviv", "phone_number": "+380"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["city"] == "Lviv"
    assert "tenant.updated" in [c["kind"] for c in client.audit_calls]  # type: ignore[attr-defined]


def test_update_tenant_denied_for_non_active_tenant(make_client, monkeypatch) -> None:
    """A write against a tenant that is not the caller's active tenant → 403."""
    from auth_service.routers import tenants

    async def _get_membership(conn, *, tenant_id, user_sub):  # noqa: ANN001
        return _membership_row("owner", tenant_id=TENANT_B)

    monkeypatch.setattr(tenants.repo, "get_membership", _get_membership)

    client = make_client(_claims(roles=["tenant_admin"], tid=TENANT_A))
    resp = client.patch(f"/tenants/{TENANT_B}", json={"city": "Lviv"})
    assert resp.status_code == 403


def test_update_tenant_denied_for_viewer_membership(make_client, monkeypatch) -> None:
    """tenant_admin JWT but only a 'doctor' (non-manager) membership → 403."""
    from auth_service.routers import tenants

    async def _get_membership(conn, *, tenant_id, user_sub):  # noqa: ANN001
        return _membership_row("doctor")

    monkeypatch.setattr(tenants.repo, "get_membership", _get_membership)

    client = make_client(_claims(roles=["tenant_admin"]))
    resp = client.patch(f"/tenants/{TENANT_A}", json={"city": "Lviv"})
    assert resp.status_code == 403


def test_update_tenant_forbidden_for_nurse_role(make_client) -> None:
    client = make_client(_claims(roles=["nurse"]))
    resp = client.patch(f"/tenants/{TENANT_A}", json={"city": "Lviv"})
    assert resp.status_code == 403


# ── Isolation / read ─────────────────────────────────────────────────────


def test_get_tenant_not_a_member_is_404(make_client, monkeypatch) -> None:
    from auth_service.routers import tenants

    async def _get_membership(conn, *, tenant_id, user_sub):  # noqa: ANN001
        return None  # caller has no membership in TENANT_B

    monkeypatch.setattr(tenants.repo, "get_membership", _get_membership)

    client = make_client(_claims(roles=["tenant_admin"], tid=TENANT_A))
    resp = client.get(f"/tenants/{TENANT_B}")
    assert resp.status_code == 404


def test_get_tenant_member_ok(make_client, monkeypatch) -> None:
    from auth_service.routers import tenants

    async def _get_membership(conn, *, tenant_id, user_sub):  # noqa: ANN001
        return _membership_row("admin")

    async def _get_tenant(conn, *, tenant_id):  # noqa: ANN001
        return _tenant_row(tenant_id)

    monkeypatch.setattr(tenants.repo, "get_membership", _get_membership)
    monkeypatch.setattr(tenants.repo, "get_tenant", _get_tenant)

    client = make_client(_claims(roles=["clinician"]))
    resp = client.get(f"/tenants/{TENANT_A}")
    assert resp.status_code == 200
    assert resp.json()["my_role"] == "admin"


def test_list_tenants_returns_memberships(make_client, monkeypatch) -> None:
    from auth_service.routers import tenants

    async def _list_for_user(conn, *, user_sub):  # noqa: ANN001
        return [
            {**_tenant_row(TENANT_A), "membership_role": "owner", "membership_status": "active"},
            {**_tenant_row(TENANT_B, name="tenant-b", slug="tenant-b"),
             "membership_role": "admin", "membership_status": "active"},
        ]

    monkeypatch.setattr(tenants.repo, "list_tenants_for_user", _list_for_user)

    client = make_client(_claims(roles=["tenant_admin"]))
    resp = client.get("/tenants")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert {i["my_role"] for i in items} == {"owner", "admin"}


# ── Members ──────────────────────────────────────────────────────────────


def test_add_member_validates_role(make_client, monkeypatch) -> None:
    from auth_service.routers import tenants

    async def _get_membership(conn, *, tenant_id, user_sub):  # noqa: ANN001
        return _membership_row("owner")

    monkeypatch.setattr(tenants.repo, "get_membership", _get_membership)

    client = make_client(_claims(roles=["tenant_admin"]))
    resp = client.post(
        f"/tenants/{TENANT_A}/members",
        json={"user_sub": str(uuid4()), "role": "superuser"},
    )
    assert resp.status_code == 422


def test_add_member_happy_path(make_client, monkeypatch) -> None:
    from auth_service.routers import tenants

    new_sub = uuid4()

    async def _get_membership(conn, *, tenant_id, user_sub):  # noqa: ANN001
        return _membership_row("owner")

    async def _add_member(conn, **kw):  # noqa: ANN001
        return _membership_row(kw["role"])

    monkeypatch.setattr(tenants.repo, "get_membership", _get_membership)
    monkeypatch.setattr(tenants.repo, "add_member", _add_member)

    client = make_client(_claims(roles=["tenant_admin"]))
    resp = client.post(
        f"/tenants/{TENANT_A}/members", json={"user_sub": str(new_sub), "role": "nurse"}
    )
    assert resp.status_code == 201, resp.text
    assert "tenant.member_added" in [c["kind"] for c in client.audit_calls]  # type: ignore[attr-defined]


def test_add_member_forbidden_for_clinician(make_client) -> None:
    client = make_client(_claims(roles=["clinician"]))
    resp = client.post(f"/tenants/{TENANT_A}/members", json={"user_sub": str(uuid4()), "role": "nurse"})
    assert resp.status_code == 403


def test_demote_last_owner_blocked(make_client, monkeypatch) -> None:
    from auth_service.routers import tenants

    target = uuid4()

    async def _get_membership(conn, *, tenant_id, user_sub):  # noqa: ANN001
        # caller is owner; target is the (only) owner being demoted
        return _membership_row("owner")

    async def _count_active_owners(conn, *, tenant_id, exclude_sub=None):  # noqa: ANN001
        return 0  # no other owners remain

    monkeypatch.setattr(tenants.repo, "get_membership", _get_membership)
    monkeypatch.setattr(tenants.repo, "count_active_owners", _count_active_owners)

    client = make_client(_claims(roles=["tenant_admin"]))
    resp = client.patch(f"/tenants/{TENANT_A}/members/{target}", json={"role": "admin"})
    assert resp.status_code == 409


def test_remove_last_owner_blocked(make_client, monkeypatch) -> None:
    from auth_service.routers import tenants

    target = uuid4()

    async def _get_membership(conn, *, tenant_id, user_sub):  # noqa: ANN001
        return _membership_row("owner")

    async def _count_active_owners(conn, *, tenant_id, exclude_sub=None):  # noqa: ANN001
        return 0

    monkeypatch.setattr(tenants.repo, "get_membership", _get_membership)
    monkeypatch.setattr(tenants.repo, "count_active_owners", _count_active_owners)

    client = make_client(_claims(roles=["tenant_admin"]))
    resp = client.delete(f"/tenants/{TENANT_A}/members/{target}")
    assert resp.status_code == 409


# ── Switch ───────────────────────────────────────────────────────────────


def test_switch_requires_membership(make_client, monkeypatch) -> None:
    from auth_service.routers import tenants

    async def _get_membership(conn, *, tenant_id, user_sub):  # noqa: ANN001
        return None

    monkeypatch.setattr(tenants.repo, "get_membership", _get_membership)
    client = make_client(_claims(roles=["clinician"], tid=TENANT_A))
    resp = client.post(f"/tenants/{TENANT_B}/switch")
    assert resp.status_code == 404


def test_switch_member_ok(make_client, monkeypatch) -> None:
    from auth_service.routers import tenants

    async def _get_membership(conn, *, tenant_id, user_sub):  # noqa: ANN001
        return _membership_row("admin", tenant_id=TENANT_B)

    async def _get_tenant(conn, *, tenant_id):  # noqa: ANN001
        return _tenant_row(tenant_id, name="tenant-b")

    monkeypatch.setattr(tenants.repo, "get_membership", _get_membership)
    monkeypatch.setattr(tenants.repo, "get_tenant", _get_tenant)

    client = make_client(_claims(roles=["clinician"], tid=TENANT_A))
    resp = client.post(f"/tenants/{TENANT_B}/switch")
    assert resp.status_code == 200
    assert resp.json()["active_tenant"]["my_role"] == "admin"
    assert "tenant.switched" in [c["kind"] for c in client.audit_calls]  # type: ignore[attr-defined]
