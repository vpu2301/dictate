"""Behavioural tests for the version-history endpoints (M1·A1/A2).

Exercises the real handlers with the auth dependency overridden and the
DB/audit boundary stubbed — no infra required (mirrors the asr-service
result-endpoint test).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims
from report_models import ReportContent

# Fixed requester so author / non-author scenarios are deterministic.
REQUESTER_SUB = UUID("11111111-1111-1111-1111-111111111111")
OTHER_AUTHOR = UUID("22222222-2222-2222-2222-222222222222")
REPORT_ID = UUID("33333333-3333-3333-3333-333333333333")
TEMPLATE_ID = UUID("44444444-4444-4444-4444-444444444444")


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


def _report_row(*, primary_author_id: UUID):
    from report_service.domain.reports_repository import ReportRow

    now = datetime(2026, 5, 20, tzinfo=UTC)
    return ReportRow(
        id=REPORT_ID,
        tenant_id=uuid4(),
        code="R-0001",
        status="finalized",  # type: ignore[arg-type]
        current_version_id=uuid4(),
        current_version_number=2,
        primary_author_id=primary_author_id,
        co_author_ids=[],
        title="Chest CT",
        icd10_codes=[],
        encounter_date=now,
        created_at=now,
        updated_at=now,
        finalized_at=now,
        signed_at=None,
        cancelled_at=None,
    )


def _summary(version_number: int):
    from report_service.domain.reports_repository import VersionSummaryRow

    return VersionSummaryRow(
        id=uuid4(),
        version_number=version_number,
        parent_version_id=None,
        created_by=REQUESTER_SUB,
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        is_amendment=False,
        amendment_type=None,
        amendment_reason=None,
        signed_at=None,
        signed_by=None,
    )


def _version_row(version_number: int):
    from report_service.domain.reports_repository import VersionRow

    return VersionRow(
        id=uuid4(),
        report_id=REPORT_ID,
        version_number=version_number,
        parent_version_id=None,
        created_by=REQUESTER_SUB,
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        content=ReportContent(template_id=TEMPLATE_ID, template_schema_version=1),
        rendered_text="rendered body",
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
    from report_service.routers import reports_versions

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

    monkeypatch.setattr(reports_versions, "tenant_connection", _fake_tenant_conn)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _clinician_claims
    c = TestClient(app)
    c.audit_calls = audit_calls  # type: ignore[attr-defined]
    return c


# ── A1: list versions ───────────────────────────────────────────────


def test_list_versions_404_when_report_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.routers import reports_versions

    async def _fetch_report(conn, *, report_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(reports_versions.repo, "fetch_report", _fetch_report)
    resp = client.get(f"/v1/reports/{REPORT_ID}/versions")
    assert resp.status_code == 404


def test_list_versions_author_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.routers import reports_versions

    async def _fetch_report(conn, *, report_id):  # noqa: ANN001
        return _report_row(primary_author_id=REQUESTER_SUB)

    async def _list(conn, *, report_id):  # noqa: ANN001
        return [_summary(1), _summary(2)]

    monkeypatch.setattr(reports_versions.repo, "fetch_report", _fetch_report)
    monkeypatch.setattr(reports_versions.repo, "list_version_summaries", _list)

    resp = client.get(f"/v1/reports/{REPORT_ID}/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert [v["version_number"] for v in body] == [1, 2]
    # A1 is a read — no audit event emitted.
    assert client.audit_calls == []  # type: ignore[attr-defined]


def test_list_versions_non_author_requires_purpose(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.routers import reports_versions

    async def _fetch_report(conn, *, report_id):  # noqa: ANN001
        return _report_row(primary_author_id=OTHER_AUTHOR)

    async def _list(conn, *, report_id):  # noqa: ANN001
        return [_summary(1)]

    monkeypatch.setattr(reports_versions.repo, "fetch_report", _fetch_report)
    monkeypatch.setattr(reports_versions.repo, "list_version_summaries", _list)

    # No purpose → 422.
    assert client.get(f"/v1/reports/{REPORT_ID}/versions").status_code == 422
    # With purpose → 200.
    resp = client.get(f"/v1/reports/{REPORT_ID}/versions?purpose=audit")
    assert resp.status_code == 200


# ── A2: one version ─────────────────────────────────────────────────


def test_get_version_404_when_version_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.routers import reports_versions

    async def _fetch_report(conn, *, report_id):  # noqa: ANN001
        return _report_row(primary_author_id=REQUESTER_SUB)

    async def _fetch_v(conn, *, report_id, version_number):  # noqa: ANN001
        return None

    monkeypatch.setattr(reports_versions.repo, "fetch_report", _fetch_report)
    monkeypatch.setattr(reports_versions.repo, "fetch_version_by_number", _fetch_v)

    resp = client.get(f"/v1/reports/{REPORT_ID}/versions/9")
    assert resp.status_code == 404


def test_get_version_ok_emits_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_service.routers import reports_versions

    async def _fetch_report(conn, *, report_id):  # noqa: ANN001
        return _report_row(primary_author_id=REQUESTER_SUB)

    async def _fetch_v(conn, *, report_id, version_number):  # noqa: ANN001
        return _version_row(version_number)

    monkeypatch.setattr(reports_versions.repo, "fetch_report", _fetch_report)
    monkeypatch.setattr(reports_versions.repo, "fetch_version_by_number", _fetch_v)

    resp = client.get(f"/v1/reports/{REPORT_ID}/versions/2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version_number"] == 2
    assert body["rendered_text"] == "rendered body"
    assert body["content"]["template_id"] == str(TEMPLATE_ID)
    # A2 emits REPORT_VIEWED_FULL carrying the version number.
    calls = client.audit_calls  # type: ignore[attr-defined]
    assert len(calls) == 1
    assert calls[0]["kind"] == "report.viewed_full"
    assert calls[0]["payload"]["version_number"] == 2
