"""Behavioural tests for ``GET /reports/{id}/pdf`` (M1·A3).

The actual weasyprint render is stubbed — these assert the finalized gate,
content negotiation and audit, not the renderer (which has its own tests in
libs/kep).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims
from report_models import ReportContent, ReportStatus

REQUESTER_SUB = UUID("11111111-1111-1111-1111-111111111111")
REPORT_ID = UUID("33333333-3333-3333-3333-333333333333")
TEMPLATE_ID = UUID("44444444-4444-4444-4444-444444444444")


def _clinician_claims() -> Claims:
    return Claims(
        sub=REQUESTER_SUB,
        tid=uuid4(),
        roles=["clinician"],
        sid="s",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )


def _report_row(*, status: ReportStatus, finalized: bool):
    from report_service.domain.reports_repository import ReportRow

    now = datetime(2026, 5, 20, tzinfo=UTC)
    return ReportRow(
        id=REPORT_ID,
        tenant_id=uuid4(),
        code="R-0001",
        status=status,
        current_version_id=uuid4(),
        current_version_number=1,
        primary_author_id=REQUESTER_SUB,
        co_author_ids=[],
        title="Chest CT",
        icd10_codes=[],
        encounter_date=now,
        created_at=now,
        updated_at=now,
        finalized_at=now if finalized else None,
        signed_at=None,
        cancelled_at=None,
    )


def _version_row():
    from report_service.domain.reports_repository import VersionRow

    return VersionRow(
        id=uuid4(),
        report_id=REPORT_ID,
        version_number=1,
        parent_version_id=None,
        created_by=REQUESTER_SUB,
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        content=ReportContent(template_id=TEMPLATE_ID, template_schema_version=1),
        rendered_text="body",
        body_hash=None,
        is_amendment=False,
        amendment_type=None,
        amendment_reason=None,
        signed_at=None,
        signed_by=None,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from report_service import deps
    from report_service.main import create_app
    from report_service.routers import reports_pdf

    audit_calls: list[dict] = []

    async def _write_event(**kwargs):  # noqa: ANN003
        audit_calls.append(kwargs)

    deps.install_state(  # type: ignore[arg-type]
        SimpleNamespace(
            app_pool=object(),
            audit_writer=SimpleNamespace(write_event=_write_event),
        )
    )

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(reports_pdf, "tenant_connection", _fake_tenant_conn)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _clinician_claims
    c = TestClient(app)
    c.audit_calls = audit_calls  # type: ignore[attr-defined]
    return c


def test_pdf_404_when_missing(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from report_service.routers import reports_pdf

    async def _fetch_report(conn, *, report_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(reports_pdf.repo, "fetch_report", _fetch_report)
    assert client.get(f"/v1/reports/{REPORT_ID}/pdf").status_code == 404


def test_pdf_409_for_draft(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from report_service.routers import reports_pdf

    async def _fetch_report(conn, *, report_id):  # noqa: ANN001
        return _report_row(status=ReportStatus.DRAFT, finalized=False)

    monkeypatch.setattr(reports_pdf.repo, "fetch_report", _fetch_report)
    resp = client.get(f"/v1/reports/{REPORT_ID}/pdf")
    assert resp.status_code == 409
    assert "report-not-finalized" in resp.text
    assert client.audit_calls == []  # type: ignore[attr-defined]


def test_pdf_200_for_finalized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.routers import reports_pdf

    async def _fetch_report(conn, *, report_id):  # noqa: ANN001
        return _report_row(status=ReportStatus.FINALIZED, finalized=True)

    async def _fetch_version(conn, *, version_id):  # noqa: ANN001
        return _version_row()

    def _render(*, report, version, issuer_name):  # noqa: ANN001
        return b"%PDF-1.7 fake-bytes"

    monkeypatch.setattr(reports_pdf.repo, "fetch_report", _fetch_report)
    monkeypatch.setattr(reports_pdf.repo, "fetch_version", _fetch_version)
    monkeypatch.setattr(reports_pdf, "render_report_pdf", _render)

    resp = client.get(f"/v1/reports/{REPORT_ID}/pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content == b"%PDF-1.7 fake-bytes"

    calls = client.audit_calls  # type: ignore[attr-defined]
    assert len(calls) == 1
    assert calls[0]["kind"] == "report.pdf_rendered"
    assert calls[0]["payload"]["size_bytes"] == len(b"%PDF-1.7 fake-bytes")
