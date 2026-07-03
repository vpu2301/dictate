"""POST /v1/reports — patient is required + tenant-scoped (defence-in-depth).

Exercises the real ``reports.create_report`` handler with the auth
dependency overridden and the DB/audit boundary stubbed — no infra
required (mirrors ``test_reports_section_labels``).
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims

REQUESTER_SUB = UUID("11111111-1111-1111-1111-111111111111")
TEMPLATE_ID = UUID("44444444-4444-4444-4444-444444444444")
PATIENT_ID = UUID("88888888-8888-8888-8888-888888888888")
REPORT_ID = UUID("33333333-3333-3333-3333-333333333333")
VERSION_ID = UUID("55555555-5555-5555-5555-555555555555")


def _clinician_claims() -> Claims:
    return Claims(
        sub=REQUESTER_SUB,
        tid=uuid4(),
        roles=["clinician"],
        sid="test-session",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )


def _content_payload() -> dict:
    return {
        "template_id": str(TEMPLATE_ID),
        "template_schema_version": 1,
        "title": "Chest CT",
        "sections": [{"section_key": "findings", "text": "Normal study"}],
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from report_service import deps
    from report_service.main import create_app
    from report_service.routers import reports

    audit_calls: list[dict] = []

    async def _write_event(**kwargs):  # noqa: ANN003
        audit_calls.append(kwargs)

    fake_state = SimpleNamespace(
        app_pool=object(),
        audit_writer=SimpleNamespace(write_event=_write_event),
    )
    deps.install_state(fake_state)  # type: ignore[arg-type]

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(reports, "tenant_connection", _fake_tenant_conn)

    async def _next_code(conn, *, tenant_id):  # noqa: ANN001
        return "REP-2026-00001"

    monkeypatch.setattr(reports.code_sequence, "next_code", _next_code)

    create_calls: list[dict] = []

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        create_calls.append(kwargs)
        return REPORT_ID, VERSION_ID

    monkeypatch.setattr(reports.repo, "create_report_with_v1", _create)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _clinician_claims
    c = TestClient(app)
    c.audit_calls = audit_calls  # type: ignore[attr-defined]
    c.create_calls = create_calls  # type: ignore[attr-defined]
    return c


def test_create_missing_patient_id_422(client: TestClient) -> None:
    # patient_id absent → Pydantic rejects before any DB work.
    resp = client.post("/v1/reports", json={"content": _content_payload()})
    assert resp.status_code == 422


def test_create_patient_not_found_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.routers import reports

    async def _no_patient(conn, *, patient_id):  # noqa: ANN001
        return None  # cross-tenant / missing → hidden by RLS

    monkeypatch.setattr(reports.repo, "fetch_patient_label", _no_patient)

    resp = client.post(
        "/v1/reports",
        json={"content": _content_payload(), "patient_id": str(PATIENT_ID)},
    )
    assert resp.status_code == 422
    # str()-wrapped dict detail; assert on the substring to avoid literal_eval.
    assert "patient_not_found" in resp.json()["detail"]
    # No report was created and no audit was emitted.
    assert client.create_calls == []  # type: ignore[attr-defined]
    assert client.audit_calls == []  # type: ignore[attr-defined]


def test_create_valid_patient_201_stores_initials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.domain.reports_repository import PatientLabel
    from report_service.routers import reports

    async def _patient(conn, *, patient_id):  # noqa: ANN001
        assert patient_id == PATIENT_ID
        return PatientLabel(id=PATIENT_ID, name_uk="Іван Петренко", name_en="Ivan Petrenko")

    monkeypatch.setattr(reports.repo, "fetch_patient_label", _patient)

    resp = client.post(
        "/v1/reports",
        json={"content": _content_payload(), "patient_id": str(PATIENT_ID)},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == str(REPORT_ID)
    assert body["status"] == "draft"

    # Redacted initials (Ukrainian name preferred) were passed to the repo.
    create = client.create_calls[0]  # type: ignore[attr-defined]
    assert create["patient_id"] == PATIENT_ID
    assert create["patient_name_redacted"] == "І.П."
