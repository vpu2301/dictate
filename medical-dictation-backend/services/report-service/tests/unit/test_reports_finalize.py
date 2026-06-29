"""Behavioural tests for POST /v1/reports/{id}/finalize (Items 2 + 5).

Exercises the real handler with the auth dependency overridden and the
DB/audit boundary stubbed — no infra required (mirrors
``test_reports_versions``).
"""

from __future__ import annotations

import ast
import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims
from report_models import ReportContent, ReportSection

REQUESTER_SUB = UUID("11111111-1111-1111-1111-111111111111")
REPORT_ID = UUID("33333333-3333-3333-3333-333333333333")
TEMPLATE_ID = UUID("44444444-4444-4444-4444-444444444444")
VERSION_ID = UUID("55555555-5555-5555-5555-555555555555")
SESSION_ID = UUID("66666666-6666-6666-6666-666666666666")

CURRENT_VERSION_NUMBER = 3


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


def _report_row(*, source_session_id: UUID | None = None):
    from report_models import ReportStatus
    from report_service.domain.reports_repository import ReportRow

    now = datetime(2026, 5, 20, tzinfo=UTC)
    return ReportRow(
        id=REPORT_ID,
        tenant_id=uuid4(),
        code="R-0001",
        status=ReportStatus.DRAFT,
        current_version_id=VERSION_ID,
        current_version_number=CURRENT_VERSION_NUMBER,
        primary_author_id=REQUESTER_SUB,
        co_author_ids=[],
        title="Chest CT",
        icd10_codes=[],
        encounter_date=now,
        created_at=now,
        updated_at=now,
        finalized_at=None,
        signed_at=None,
        cancelled_at=None,
        source_session_id=source_session_id,
    )


def _version_row(sections):
    from report_service.domain.reports_repository import VersionRow

    return VersionRow(
        id=VERSION_ID,
        report_id=REPORT_ID,
        version_number=CURRENT_VERSION_NUMBER,
        parent_version_id=None,
        created_by=REQUESTER_SUB,
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        content=ReportContent(
            template_id=TEMPLATE_ID,
            template_schema_version=1,
            sections=sections,
        ),
        rendered_text="rendered body",
        body_hash=None,
        is_amendment=False,
        amendment_type=None,
        amendment_reason=None,
        signed_at=None,
        signed_by=None,
    )


def _template(sections):
    return SimpleNamespace(sections=[SimpleNamespace(**s) for s in sections])


def _detail(resp):
    """Recover the (dict) HTTPException detail from the RFC 9457 envelope.

    The service's global handler str()-wraps dict details into the
    ``detail`` member, so we round-trip via ``ast.literal_eval``.
    """
    return ast.literal_eval(resp.json()["detail"])


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from report_service import deps
    from report_service.main import create_app
    from report_service.routers import reports_lifecycle

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

    monkeypatch.setattr(reports_lifecycle, "tenant_connection", _fake_tenant_conn)

    # No-op state-machine transition (DB-free).
    async def _finalize(conn, *, report_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(reports_lifecycle._sm, "finalize", _finalize)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _clinician_claims
    c = TestClient(app)
    c.audit_calls = audit_calls  # type: ignore[attr-defined]
    return c


def _wire(monkeypatch, *, row, version, template_sections):
    from report_service.routers import reports_lifecycle

    async def _lock(conn, *, report_id):  # noqa: ANN001
        return row

    async def _fetch_v(conn, *, version_id):  # noqa: ANN001
        return version

    async def _tpl(conn, *, template_id):  # noqa: ANN001
        return _template(template_sections)

    set_calls: list[dict] = []

    async def _set_session(conn, *, report_id, session_id):  # noqa: ANN001
        set_calls.append({"report_id": report_id, "session_id": session_id})

    monkeypatch.setattr(reports_lifecycle.repo, "lock_report_for_update", _lock)
    monkeypatch.setattr(reports_lifecycle.repo, "fetch_version", _fetch_v)
    monkeypatch.setattr(reports_lifecycle, "_fetch_template_definition", _tpl)
    monkeypatch.setattr(
        reports_lifecycle.repo, "set_source_session_id_if_absent", _set_session
    )
    return set_calls


def test_finalize_422_problems_include_section_key_and_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(
        monkeypatch,
        row=_report_row(),
        version=_version_row([]),  # required section absent
        template_sections=[
            {"key": "ccx", "required": True, "min_chars": 0, "icd10_required": False}
        ],
    )

    resp = client.post(f"/v1/reports/{REPORT_ID}/finalize")
    assert resp.status_code == 422
    # `problems` is now a first-class RFC-9457 extension member (top-level JSON),
    # not stuffed into the str()-wrapped `detail`.
    payload = resp.json()
    assert payload["code"] == "finalize_validation_failed"
    problems = payload["problems"]
    assert len(problems) == 1
    p = problems[0]
    assert p["section_key"] == "ccx"
    assert p["reason"] == "required_empty"
    # Backward-compat keys retained.
    assert p["code"] == "missing_required_section"
    assert p["field"] == "sections.ccx.text"


def test_finalize_stale_expected_version_conflicts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(
        monkeypatch,
        row=_report_row(),
        version=_version_row([ReportSection(section_key="ccx", text="ok")]),
        template_sections=[],
    )

    resp = client.post(
        f"/v1/reports/{REPORT_ID}/finalize",
        json={"expected_version": CURRENT_VERSION_NUMBER - 1},
    )
    assert resp.status_code == 409
    assert _detail(resp)["error"] == "optimistic_lock_mismatch"
    assert client.audit_calls == []  # type: ignore[attr-defined]


def test_finalize_correct_expected_version_succeeds_and_emits_completed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_calls = _wire(
        monkeypatch,
        row=_report_row(source_session_id=None),
        version=_version_row(
            [
                ReportSection(section_key="findings", text="Normal [[uncertain]] study"),
                ReportSection(section_key="impression", text="Clear"),
            ]
        ),
        template_sections=[],
    )

    resp = client.post(
        f"/v1/reports/{REPORT_ID}/finalize",
        json={
            "expected_version": CURRENT_VERSION_NUMBER,
            "dictation_session_id": str(SESSION_ID),
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "finalized"

    # Session linkage was persisted (report had none).
    assert set_calls == [{"report_id": REPORT_ID, "session_id": SESSION_ID}]

    calls = client.audit_calls  # type: ignore[attr-defined]
    kinds = [c["kind"] for c in calls]
    assert "report.finalized" in kinds
    assert "report.completed" in kinds

    completed = next(c for c in calls if c["kind"] == "report.completed")
    assert completed["target_id"] == str(REPORT_ID)
    payload = completed["payload"]
    assert payload["version_number"] == CURRENT_VERSION_NUMBER
    assert payload["section_count"] == 2
    assert payload["low_confidence_count"] == 1
    assert payload["source_session_id"] == str(SESSION_ID)


def test_finalize_no_body_backward_compatible(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_calls = _wire(
        monkeypatch,
        row=_report_row(source_session_id=SESSION_ID),
        version=_version_row([ReportSection(section_key="ccx", text="ok")]),
        template_sections=[],
    )

    resp = client.post(f"/v1/reports/{REPORT_ID}/finalize")
    assert resp.status_code == 200
    # No dictation_session_id supplied → no backfill attempt.
    assert set_calls == []

    completed = next(
        c for c in client.audit_calls if c["kind"] == "report.completed"  # type: ignore[attr-defined]
    )
    # Existing source session is surfaced in the payload.
    assert completed["payload"]["source_session_id"] == str(SESSION_ID)
