"""S21 — MFA reminders: the access review's one action.

The whole point of this endpoint is that an `auditor` — a role that may
not invite, deactivate, change a role or reset a credential — can raise a
standing request that a user enrols a second factor. So the tests below
are as much about what the act does NOT do as about what it does:

  · an auditor may raise one (the only write in their whole matrix row),
  · a clinician and a nurse may not,
  · it refuses a user who is already enrolled, deactivated, or is you,
  · a repeat ask escalates the SAME row rather than stacking rows,
  · every ask lands on the audit trail at `sec`.

The DB is doubled with a dict standing in for `mfa_reminders`, which is
enough to assert the upsert's shape (one row per user, count climbing)
without a live Postgres — the migration's own constraints are covered by
the schema tests.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims

TENANT = UUID("00000000-0000-0000-0000-00000000000a")
AUDITOR = UUID("0a000000-0000-0000-0000-00000000aa01")
ADMIN = UUID("0a000000-0000-0000-0000-00000000aa02")
TARGET = UUID("0a000000-0000-0000-0000-00000000bb01")
ENROLLED = UUID("0a000000-0000-0000-0000-00000000bb02")
GONE = UUID("0a000000-0000-0000-0000-00000000bb03")

T0 = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)


def _claims(*, roles: list[str], sub: UUID) -> Claims:
    return Claims(
        sub=sub,
        tid=TENANT,
        roles=roles,
        scope="",
        mfa=True,
        mfa_enrolled=True,
        sid="sid-1",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )


class FakeDb:
    """The two tables this endpoint touches, as dicts."""

    def __init__(self) -> None:
        self.users: dict[UUID, dict[str, Any]] = {
            AUDITOR: {"status": "active", "mfa_enrolled_at": T0},
            ADMIN: {"status": "active", "mfa_enrolled_at": T0},
            TARGET: {"status": "active", "mfa_enrolled_at": None},
            ENROLLED: {"status": "active", "mfa_enrolled_at": T0},
            GONE: {"status": "deactivated", "mfa_enrolled_at": None},
        }
        self.reminders: dict[UUID, dict[str, Any]] = {}
        self.now = T0

    # ── the two statements the router issues ────────────────────────
    def select_user(self, sub: UUID) -> dict[str, Any] | None:
        row = self.users.get(sub)
        if row is None:
            return None
        return {"sub": sub, **row}

    def upsert_reminder(self, sub: UUID, by: UUID, role: str) -> dict[str, Any]:
        self.now += timedelta(minutes=1)
        cur = self.reminders.get(sub)
        if cur is None or cur.get("resolved_at") is not None:
            cur = {
                "first_reminded_at": cur["first_reminded_at"] if cur else self.now,
                "reminder_count": (cur["reminder_count"] if cur else 0) + 1,
            }
        else:
            cur = {**cur, "reminder_count": cur["reminder_count"] + 1}
        cur.update(
            requested_by=by,
            requested_by_role=role,
            last_reminded_at=self.now,
            resolved_at=None,
        )
        self.reminders[sub] = cur
        return cur


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from auth_service import deps
    from auth_service.main import create_app
    from auth_service.routers import admin as admin_router

    db = FakeDb()
    audit_calls: list[dict[str, Any]] = []
    published: list[dict[str, Any]] = []

    async def _write_event(**kwargs: Any) -> None:
        audit_calls.append(kwargs)

    async def _emit(_bus: Any, **kwargs: Any) -> None:
        published.append(kwargs)

    monkeypatch.setattr(admin_router, "emit_mfa_reminder", _emit)

    state = SimpleNamespace(
        audit_writer=SimpleNamespace(write_event=_write_event),
        app_pool=object(),
        tenant_writer_pool=object(),
        notification_bus=object(),
    )
    deps.install_state(state)  # type: ignore[arg-type]

    @contextlib.asynccontextmanager
    async def _fake_conn(pool: Any, tenant_id: Any):
        async def _fetchrow(query: str, *a: Any) -> Any:
            if "FROM users" in query:
                return db.select_user(a[0])
            if "INSERT INTO mfa_reminders" in query:
                return db.upsert_reminder(a[1], a[2], a[3])
            raise AssertionError(f"unexpected query: {query}")

        yield SimpleNamespace(fetchrow=_fetchrow)

    monkeypatch.setattr(admin_router, "tenant_connection", _fake_conn)

    def _build(claims: Claims) -> TestClient:
        app = create_app()
        app.dependency_overrides[deps.current_user] = lambda: claims
        c = TestClient(app)
        c.db = db  # type: ignore[attr-defined]
        c.audit_calls = audit_calls  # type: ignore[attr-defined]
        c.published = published  # type: ignore[attr-defined]
        return c

    return _build


def _auditor(make_client: Any) -> TestClient:
    return make_client(_claims(roles=["auditor"], sub=AUDITOR))


# ── the grant ───────────────────────────────────────────────────────────


def test_auditor_can_raise_a_reminder(make_client: Any) -> None:
    from audit import Severity

    client = _auditor(make_client)
    r = client.post(f"/admin/users/{TARGET}/mfa-reminder")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sub"] == str(TARGET)
    assert body["reminder_count"] == 1
    assert body["first_reminded_at"] == body["last_reminded_at"]

    events = [c for c in client.audit_calls if c["kind"] == "user.mfa_reminded"]
    assert len(events) == 1
    assert events[0]["severity"] == Severity.SEC
    assert events[0]["actor_sub"] == AUDITOR
    assert events[0]["actor_role"] == "auditor"
    assert events[0]["target_id"] == str(TARGET)

    # …and the arriving half went to the subject alone, carrying a role
    # rather than a name.
    assert len(client.published) == 1
    assert client.published[0]["subject_sub"] == TARGET
    assert client.published[0]["actor_role"] == "auditor"


def test_a_second_ask_escalates_the_same_row(make_client: Any) -> None:
    client = _auditor(make_client)
    first = client.post(f"/admin/users/{TARGET}/mfa-reminder").json()
    second = client.post(f"/admin/users/{TARGET}/mfa-reminder").json()

    assert second["reminder_count"] == 2
    # One standing finding, not two: the count is the escalation record.
    assert len(client.db.reminders) == 1
    assert second["first_reminded_at"] == first["first_reminded_at"]
    assert second["last_reminded_at"] > first["last_reminded_at"]
    assert len(client.published) == 2
    assert client.published[1]["reminder_count"] == 2


def test_tenant_admin_may_also_remind_and_is_recorded_as_admin(make_client: Any) -> None:
    client = make_client(_claims(roles=["tenant_admin"], sub=ADMIN))
    assert client.post(f"/admin/users/{TARGET}/mfa-reminder").status_code == 201
    assert client.db.reminders[TARGET]["requested_by_role"] == "tenant_admin"


def test_an_auditor_who_also_administers_is_recorded_as_the_auditor(
    make_client: Any,
) -> None:
    # Precedence, not alphabetical luck: the reminder is an access-review
    # act, so the review role is the one the finding is filed under.
    client = make_client(_claims(roles=["tenant_admin", "auditor"], sub=ADMIN))
    assert client.post(f"/admin/users/{TARGET}/mfa-reminder").status_code == 201
    assert client.db.reminders[TARGET]["requested_by_role"] == "auditor"


# ── the refusals ────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["clinician", "nurse", "knowledge_admin"])
def test_clinical_roles_cannot_remind(make_client: Any, role: str) -> None:
    client = make_client(_claims(roles=[role], sub=ADMIN))
    assert client.post(f"/admin/users/{TARGET}/mfa-reminder").status_code == 403


def test_already_enrolled_is_a_conflict_not_a_no_op(make_client: Any) -> None:
    client = _auditor(make_client)
    r = client.post(f"/admin/users/{ENROLLED}/mfa-reminder")
    assert r.status_code == 409
    assert client.db.reminders == {}
    assert client.published == []


def test_deactivated_user_is_a_conflict(make_client: Any) -> None:
    client = _auditor(make_client)
    assert client.post(f"/admin/users/{GONE}/mfa-reminder").status_code == 409


def test_cannot_remind_yourself(make_client: Any) -> None:
    client = _auditor(make_client)
    r = client.post(f"/admin/users/{AUDITOR}/mfa-reminder")
    assert r.status_code == 422
    assert client.db.reminders == {}


def test_unknown_user_is_404(make_client: Any) -> None:
    client = _auditor(make_client)
    assert client.post(f"/admin/users/{uuid4()}/mfa-reminder").status_code == 404


def test_the_reminder_is_not_mfa_gated(
    make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bootstrap case: a clinic where nobody has enrolled yet.

    Every other admin mutation demands a verified-MFA session. If this one
    did too, the first reviewer would need a second factor to ask anyone
    else for a second factor, and a clinic with none could never start.
    """
    from auth_service.config import settings

    monkeypatch.setattr(settings, "require_mfa", True)
    claims = _claims(roles=["auditor"], sub=AUDITOR)
    object.__setattr__(claims, "mfa", False)
    object.__setattr__(claims, "mfa_enrolled", False)
    client = make_client(claims)
    assert client.post(f"/admin/users/{TARGET}/mfa-reminder").status_code == 201
