"""core-service unit-test scaffolding.

Tests exercise the real FastAPI handlers with the auth dependency overridden
and the DB/audit boundary stubbed — no infra required (mirrors report-service).
Stubbed repositories return plain ``dict`` rows; the serializers use mapping
access, so a dict stands in for an ``asyncpg.Record``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from auth import Claims

REQUESTER_SUB = UUID("11111111-1111-1111-1111-111111111111")
TENANT_ID = UUID("00000000-0000-0000-0000-0000000000aa")


def make_claims(roles: list[str]) -> Claims:
    return Claims(
        sub=REQUESTER_SUB,
        tid=TENANT_ID,
        roles=roles,
        sid="test-session",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[str]], TestClient]:
    """Return a factory that builds a TestClient authenticated as ``roles``.

    Every router module's ``tenant_connection`` is replaced with a no-op
    async context manager, and a fake ServiceState (app_pool + capturing
    audit writer) is installed.
    """
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from core_service import deps
    from core_service.main import create_app
    from core_service.routers import (
        _phi_access_guard,
        anamnesis,
        consents,
        encounters,
        notes,
        patients,
        privacy,
    )

    audit_calls: list[dict] = []

    async def _write_event(**kwargs: object) -> None:
        audit_calls.append(kwargs)

    fake_state = SimpleNamespace(
        app_pool=object(),
        audit_writer=SimpleNamespace(write_event=_write_event),
    )
    deps.install_state(fake_state)  # type: ignore[arg-type]

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    for mod in (
        patients,
        encounters,
        notes,
        consents,
        anamnesis,
        privacy,
        _phi_access_guard,
    ):
        monkeypatch.setattr(mod, "tenant_connection", _fake_tenant_conn)

    # ── Treatment relationship ────────────────────────────────────────
    # Since the break-glass hotfix, opening one patient's record needs
    # BOTH `patient.read_full` and a treatment relationship with that
    # patient. Most tests here are about what a handler returns, not
    # about who may reach it, so the default is "related" — the ordinary
    # clinical case, and what these tests assumed implicitly before the
    # relationship existed as a concept.
    #
    # Tests that care about the access decision set
    # ``client.relationship = NO_RELATIONSHIP`` (or any other basis) and
    # get the unrelated-clinician / admin path. The predicate itself is
    # tested in libs/clinical_access; this only steers it.
    from clinical_access import Relationship, RelationshipBasis

    relationship_box: dict[str, Relationship] = {
        "value": Relationship(basis=RelationshipBasis.AUTHOR)
    }

    async def _fake_relationship(conn, *, user_sub, patient_id):  # noqa: ANN001
        return relationship_box["value"]

    monkeypatch.setattr(
        _phi_access_guard, "relationship_with_patient", _fake_relationship
    )

    # ── Break-glass grants ────────────────────────────────────────────
    # An unrelated caller falls through to the grant lookup. Default is
    # "no live grant", so the unrelated path ends in the 403 that offers
    # the request dialog. A test that wants the granted branch puts a row
    # in ``client.live_grant``.
    grant_box: dict[str, dict | None] = {"value": None}
    grant_uses: list[UUID] = []

    async def _find_live_patient_grant(conn, *, user_sub, patient_id):  # noqa: ANN001
        return grant_box["value"]

    async def _record_grant_use(conn, *, grant_id):  # noqa: ANN001
        grant_uses.append(grant_id)

    monkeypatch.setattr(
        _phi_access_guard.grants,
        "find_live_patient_grant",
        _find_live_patient_grant,
    )
    monkeypatch.setattr(
        _phi_access_guard.grants, "record_grant_use", _record_grant_use
    )

    app = create_app()

    def _factory(roles: list[str]) -> TestClient:
        app.dependency_overrides[deps.current_user] = lambda: make_claims(roles)
        c = TestClient(app)
        c.audit_calls = audit_calls  # type: ignore[attr-defined]
        c.relationship = relationship_box  # type: ignore[attr-defined]
        c.live_grant = grant_box  # type: ignore[attr-defined]
        c.grant_uses = grant_uses  # type: ignore[attr-defined]
        return c

    return _factory


@pytest.fixture
def client(make_client: Callable[[list[str]], TestClient]) -> TestClient:
    return make_client(["clinician"])
