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

    for mod in (patients, encounters, notes, consents, anamnesis, privacy):
        monkeypatch.setattr(mod, "tenant_connection", _fake_tenant_conn)

    app = create_app()

    def _factory(roles: list[str]) -> TestClient:
        app.dependency_overrides[deps.current_user] = lambda: make_claims(roles)
        c = TestClient(app)
        c.audit_calls = audit_calls  # type: ignore[attr-defined]
        return c

    return _factory


@pytest.fixture
def client(make_client: Callable[[list[str]], TestClient]) -> TestClient:
    return make_client(["clinician"])
