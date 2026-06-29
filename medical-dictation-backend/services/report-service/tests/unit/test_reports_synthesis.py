"""Report synthesis endpoints + mock engine (spec item 1).

No infra: the auth dep is overridden and the DB/audit boundary is stubbed
with an in-memory job store (mirrors ``test_reports_section_labels``).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims
from report_models import ReportContent, ReportSection, ReportStatus

REQUESTER_SUB = UUID("11111111-1111-1111-1111-111111111111")
REPORT_ID = UUID("33333333-3333-3333-3333-333333333333")
TEMPLATE_ID = UUID("44444444-4444-4444-4444-444444444444")

RAW_FINDINGS = "  the   patient has [[severe]] pain.  no   fever"
CLEAN_FINDINGS = "The patient has [[severe]] pain. No fever"


def _claims() -> Claims:
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


def _content() -> ReportContent:
    return ReportContent(
        template_id=TEMPLATE_ID,
        template_schema_version=1,
        sections=[
            ReportSection(section_key="findings", text=RAW_FINDINGS),
            ReportSection(section_key="impression", text="stable"),
        ],
    )


def _report_row():
    from report_service.domain.reports_repository import ReportRow

    now = datetime(2026, 5, 20, tzinfo=UTC)
    return ReportRow(
        id=REPORT_ID,
        tenant_id=uuid4(),
        code="R-0001",
        status=ReportStatus.DRAFT,
        current_version_id=uuid4(),
        current_version_number=3,
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
    )


def _version_row():
    from report_service.domain.reports_repository import VersionRow

    return VersionRow(
        id=uuid4(),
        report_id=REPORT_ID,
        version_number=3,
        parent_version_id=None,
        created_by=REQUESTER_SUB,
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        content=_content(),
        rendered_text="rendered",
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
    from report_service.domain import synthesis_jobs
    from report_service.main import create_app
    from report_service.routers import reports_synthesis

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

    monkeypatch.setattr(reports_synthesis, "tenant_connection", _fake_tenant_conn)

    async def _fetch_report(conn, *, report_id):  # noqa: ANN001
        return _report_row()

    async def _fetch_version(conn, *, version_id):  # noqa: ANN001
        return _version_row()

    async def _no_template(conn, *, template_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(reports_synthesis.repo, "fetch_report", _fetch_report)
    monkeypatch.setattr(reports_synthesis.repo, "fetch_version", _fetch_version)

    # Read-only guarantee: no version is ever appended during synthesis.
    async def _no_append(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("synthesize must not append a version")

    monkeypatch.setattr(reports_synthesis.repo, "append_version", _no_append)

    from report_service.domain import repository

    monkeypatch.setattr(repository, "get_template", _no_template)

    # In-memory synthesis-job store keyed by request_hash for idempotency.
    by_hash: dict[tuple, UUID] = {}
    by_id: dict[UUID, object] = {}

    async def _find_completed(conn, *, report_id, request_hash):  # noqa: ANN001
        job_id = by_hash.get((report_id, request_hash))
        return by_id.get(job_id) if job_id else None

    async def _insert(conn, *, tenant_id, report_id, version_number, language, sections, request_hash, status="completed"):  # noqa: ANN001
        job_id = uuid4()
        by_id[job_id] = synthesis_jobs.SynthesisJobRow(
            id=job_id,
            report_id=report_id,
            version_number=version_number,
            language=language,
            status=status,
            sections=list(sections),
        )
        by_hash[(report_id, request_hash)] = job_id
        return job_id

    async def _fetch_job(conn, *, job_id, report_id):  # noqa: ANN001
        job = by_id.get(job_id)
        return job if job and job.report_id == report_id else None

    monkeypatch.setattr(synthesis_jobs, "find_completed_job", _find_completed)
    monkeypatch.setattr(synthesis_jobs, "insert_job", _insert)
    monkeypatch.setattr(synthesis_jobs, "fetch_job", _fetch_job)

    # Reset the cached synthesizer so config changes (if any) take effect.
    reports_synthesis.get_synthesizer.cache_clear()

    app = create_app()
    app.dependency_overrides[deps.current_user] = _claims
    c = TestClient(app)
    c.audit_calls = audit_calls  # type: ignore[attr-defined]
    return c


def test_post_synthesize_returns_original_and_clean_text(client: TestClient) -> None:
    resp = client.post(f"/v1/reports/{REPORT_ID}/synthesize", json={"language": "en"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    by_key = {s["section_key"]: s for s in body["sections"]}
    # Both sections present (default = all).
    assert set(by_key) == {"findings", "impression"}
    # Original dictation preserved verbatim; marker preserved in clean text.
    assert by_key["findings"]["original"] == RAW_FINDINGS
    assert by_key["findings"]["text"] == CLEAN_FINDINGS
    assert "[[severe]]" in by_key["findings"]["text"]
    # Audit: started + completed both emitted.
    kinds = [c["kind"] for c in client.audit_calls]  # type: ignore[attr-defined]
    assert "report.synthesis_started" in kinds
    assert "report.synthesis_completed" in kinds


def test_synthesize_is_idempotent(client: TestClient) -> None:
    first = client.post(f"/v1/reports/{REPORT_ID}/synthesize", json={"language": "en"})
    second = client.post(f"/v1/reports/{REPORT_ID}/synthesize", json={"language": "en"})
    assert first.status_code == second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]


def test_get_synthesis_job_returns_stored_result(client: TestClient) -> None:
    post = client.post(f"/v1/reports/{REPORT_ID}/synthesize", json={"language": "en"})
    job_id = post.json()["job_id"]
    got = client.get(f"/v1/reports/{REPORT_ID}/synthesize/{job_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["status"] == "completed"
    by_key = {s["section_key"]: s for s in body["sections"]}
    assert by_key["findings"]["text"] == CLEAN_FINDINGS
    assert by_key["findings"]["original"] == RAW_FINDINGS


def test_synthesize_does_not_mutate_report(client: TestClient) -> None:
    # The fixture wires repo.append_version to raise if called; a clean 200
    # plus the original dictation echoed back proves synthesis is read-only.
    resp = client.post(
        f"/v1/reports/{REPORT_ID}/synthesize",
        json={"language": "en", "sections": ["findings"]},
    )
    assert resp.status_code == 200
    sections = resp.json()["sections"]
    assert [s["section_key"] for s in sections] == ["findings"]
    assert sections[0]["original"] == RAW_FINDINGS


def test_mock_synthesizer_deterministic_and_preserves_markers() -> None:
    from report_service.domain.synthesis import MockSynthesizer

    eng = MockSynthesizer()
    kwargs = {
        "section_key": "findings",
        "raw_text": RAW_FINDINGS,
        "synthesis_prompt": "",
        "asr_prompt": "",
        "language": "en",
    }
    out1 = eng.synthesize_section(**kwargs)
    out2 = eng.synthesize_section(**kwargs)
    assert out1 == out2 == CLEAN_FINDINGS
    assert "[[severe]]" in out1


def test_anthropic_synthesizer_is_a_stub() -> None:
    from report_service.domain.synthesis import AnthropicSynthesizer, build_synthesizer

    eng = build_synthesizer("anthropic", "claude-opus-4-8")
    assert isinstance(eng, AnthropicSynthesizer)
    with pytest.raises(NotImplementedError):
        eng.synthesize_section(
            section_key="findings",
            raw_text="x",
            synthesis_prompt="",
            asr_prompt="",
            language="en",
        )
