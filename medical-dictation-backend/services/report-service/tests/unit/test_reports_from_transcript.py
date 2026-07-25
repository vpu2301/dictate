"""Assign-transcription surface: template auto-match + POST /v1/reports/from-transcript.

Mirrors ``test_reports_create``: real handlers, auth overridden, DB /
asr-service boundaries stubbed — no infra required.
"""

from __future__ import annotations

import contextlib
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from auth import Claims
from report_service.domain.reports_repository import _parse_iso_date
from report_service.domain.template_match import (
    TemplateCandidate,
    select_template,
)
from template_models import TemplateDefinition

REQUESTER_SUB = UUID("11111111-1111-1111-1111-111111111111")
PATIENT_ID = UUID("88888888-8888-8888-8888-888888888888")
JOB_ID = UUID("99999999-9999-9999-9999-999999999999")
REPORT_ID = UUID("33333333-3333-3333-3333-333333333333")
VERSION_ID = UUID("55555555-5555-5555-5555-555555555555")

XRAY_TRANSCRIPT = (
    "Рентгенографія органів грудної клітки в прямій проекції.\n"
    "На рентгенографії візуалізується інфільтративне затемнення справа.\n"
    "Висновок: бронхопневмонія."
)


def _definition(
    code: str, name: str, specialty: str, sections: list[tuple[str, str]]
) -> TemplateDefinition:
    return TemplateDefinition.model_validate(
        {
            "code": code,
            "name": name,
            "language": "uk",
            "specialty": specialty,
            "sections": [
                {
                    "id": sid,
                    "name": sname,
                    "asr_prompt": sname,
                    "order": i,
                }
                for i, (sid, sname) in enumerate(sections)
            ],
        }
    )


def _candidate(code: str, name: str, specialty: str) -> TemplateCandidate:
    return TemplateCandidate(
        id=uuid4(),
        code=code,
        name=name,
        specialty=specialty,
        schema_version=1,
        definition=_definition(
            code, name, specialty, [("findings", "Знахідки"), ("conclusion", "Висновок")]
        ),
        is_system=True,
    )


CATALOGUE = [
    _candidate("radiography_uk", "Рентгенографія", "radiology"),
    _candidate("ct_uk", "Комп'ютерна томографія", "radiology"),
    _candidate("internal_medicine_uk", "Терапевтичний прийом", "internal_medicine"),
    _candidate("cardiology_uk", "Кардіологічна консультація", "cardiology"),
]


# ── encounter_date binding (regression: asyncpg $n::date needs a date) ──


def test_parse_iso_date_returns_date_object() -> None:
    # A bare ISO string reaching a $n::date param raised
    # "'str' object has no attribute 'toordinal'" — must be a date now.
    assert _parse_iso_date("2026-07-19") == date(2026, 7, 19)


def test_parse_iso_date_none_and_empty() -> None:
    assert _parse_iso_date(None) is None
    assert _parse_iso_date("") is None


def test_parse_iso_date_invalid_is_null_not_crash() -> None:
    assert _parse_iso_date("19/07/2026") is None


# ── Auto-match (pure) ───────────────────────────────────────────────


def test_xray_transcript_matches_radiography_template() -> None:
    choice = select_template(CATALOGUE, XRAY_TRANSCRIPT)
    assert choice is not None
    assert choice.candidate.code == "radiography_uk"
    assert choice.mode == "auto"
    assert choice.score >= 3


def test_unrelated_transcript_falls_back_to_general_visit() -> None:
    choice = select_template(CATALOGUE, "пацієнт скаржиться на втому")
    assert choice is not None
    assert choice.candidate.code == "internal_medicine_uk"
    assert choice.mode == "fallback"


def test_empty_catalogue_returns_none() -> None:
    assert select_template([], XRAY_TRANSCRIPT) is None


def test_inflected_form_still_matches() -> None:
    # «томографії» (genitive) must match template «Комп'ютерна томографія».
    choice = select_template(CATALOGUE, "на комп'ютерній томографії ознаки процесу")
    assert choice is not None
    assert choice.candidate.code == "ct_uk"
    assert choice.mode == "auto"


# ── Endpoint ────────────────────────────────────────────────────────


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


def _transcript_result() -> dict:
    return {
        "job_id": str(JOB_ID),
        "language": "uk",
        "nlp_applied": True,
        "segments": [
            {"text": "Рентгенографія органів грудної клітки.", "raw_text": "…"},
            {"text": "Висновок: бронхопневмонія.", "raw_text": "…"},
        ],
    }


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from report_service import deps
    from report_service.main import create_app
    from report_service.routers import reports_from_transcript as rft

    audit_calls: list[dict] = []

    async def _write_event(**kwargs):  # noqa: ANN003
        audit_calls.append(kwargs)

    fake_state = SimpleNamespace(
        app_pool=object(),
        audit_writer=SimpleNamespace(write_event=_write_event),
    )
    deps.install_state(fake_state)  # type: ignore[arg-type]

    conn = SimpleNamespace()
    existing_reports: dict[UUID, dict] = {}

    async def _fetchrow(query, *args):  # noqa: ANN001, ANN002
        row = existing_reports.get(args[0])
        return row

    conn.fetchrow = _fetchrow

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield conn

    monkeypatch.setattr(rft, "tenant_connection", _fake_tenant_conn)

    async def _fetch_transcript(job_id, *, auth_header):  # noqa: ANN001
        return _transcript_result()

    monkeypatch.setattr(rft, "_fetch_transcript", _fetch_transcript)

    async def _fetch_patient(conn, *, patient_id):  # noqa: ANN001
        if patient_id != PATIENT_ID:
            return None
        return SimpleNamespace(id=patient_id, name_uk="Тарас Шевченко", name_en=None)

    monkeypatch.setattr(rft.repo, "fetch_patient_label", _fetch_patient)

    async def _load_candidates(conn, *, language):  # noqa: ANN001
        return CATALOGUE

    monkeypatch.setattr(rft.template_match, "load_candidates", _load_candidates)

    async def _next_code(conn, *, tenant_id):  # noqa: ANN001
        return "REP-2026-00042"

    monkeypatch.setattr(rft.code_sequence, "next_code", _next_code)

    create_calls: list[dict] = []

    async def _create(conn, **kwargs):  # noqa: ANN001, ANN003
        create_calls.append(kwargs)
        return REPORT_ID, VERSION_ID

    monkeypatch.setattr(rft.repo, "create_report_with_v1", _create)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _clinician_claims
    return SimpleNamespace(
        client=TestClient(app),
        create_calls=create_calls,
        audit_calls=audit_calls,
        existing_reports=existing_reports,
        module=rft,
        monkeypatch=monkeypatch,
    )


def test_assign_auto_selects_template_and_creates_report(rig: SimpleNamespace) -> None:
    resp = rig.client.post(
        "/v1/reports/from-transcript",
        json={"asr_job_id": str(JOB_ID), "patient_id": str(PATIENT_ID)},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["template_selection"] == "auto"
    assert body["template_name"] == "Рентгенографія"
    assert body["code"] == "REP-2026-00042"
    assert body["patient_id"] == str(PATIENT_ID)

    (call,) = rig.create_calls
    assert call["source_asr_job_id"] == JOB_ID
    content = call["content"]
    # Transcript lands in the FIRST free-text section; the rest stay default.
    assert content.sections[0].section_key == "findings"
    # Sentences joined with a space (not a newline the editor would drop),
    # so periods keep a following space rather than gluing.
    assert (
        content.sections[0].text
        == "Рентгенографія органів грудної клітки. Висновок: бронхопневмонія."
    )
    assert content.sections[1].text == ""
    assert call["patient_name_redacted"] != "Тарас Шевченко"  # initials only

    (event,) = rig.audit_calls
    assert event["payload"]["source_asr_job_id"] == str(JOB_ID)
    assert event["payload"]["template_selection"] == "auto"


def test_assign_duplicate_job_conflicts(rig: SimpleNamespace) -> None:
    rig.existing_reports[JOB_ID] = {"id": REPORT_ID, "code": "REP-2026-00001"}
    resp = rig.client.post(
        "/v1/reports/from-transcript",
        json={"asr_job_id": str(JOB_ID), "patient_id": str(PATIENT_ID)},
    )
    assert resp.status_code == 409
    assert "already_assigned" in resp.text
    assert rig.create_calls == []


def test_assign_unknown_patient_422(rig: SimpleNamespace) -> None:
    resp = rig.client.post(
        "/v1/reports/from-transcript",
        json={"asr_job_id": str(JOB_ID), "patient_id": str(uuid4())},
    )
    assert resp.status_code == 422
    assert "patient_not_found" in resp.text


def test_assign_passes_through_not_ready_409(rig: SimpleNamespace) -> None:
    async def _not_ready(job_id, *, auth_header):  # noqa: ANN001
        raise HTTPException(409, detail={"job_status": "running"})

    rig.monkeypatch.setattr(rig.module, "_fetch_transcript", _not_ready)
    resp = rig.client.post(
        "/v1/reports/from-transcript",
        json={"asr_job_id": str(JOB_ID), "patient_id": str(PATIENT_ID)},
    )
    assert resp.status_code == 409
    assert rig.create_calls == []


def test_by_source_job_bulk_lookup(rig: SimpleNamespace) -> None:
    async def _fetch_links(conn, *, asr_job_ids):  # noqa: ANN001
        assert asr_job_ids == [JOB_ID]
        return [
            {
                "source_asr_job_id": JOB_ID,
                "id": REPORT_ID,
                "code": "REP-2026-00042",
                "status": "draft",
                "patient_id": PATIENT_ID,
            }
        ]

    rig.monkeypatch.setattr(rig.module.repo, "fetch_reports_by_source_jobs", _fetch_links)
    resp = rig.client.get(f"/v1/reports/by-source-job?ids={JOB_ID}")
    assert resp.status_code == 200
    (link,) = resp.json()
    assert link["report_id"] == str(REPORT_ID)
    assert link["asr_job_id"] == str(JOB_ID)
