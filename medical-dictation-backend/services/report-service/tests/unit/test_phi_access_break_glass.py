"""S14 break-glass — the admin ⟂ PHI split and the door through it.

Exercises the real handlers with the auth dependency overridden and the
DB/audit/bus boundaries stubbed, mirroring ``test_reports_create``.

What these pin, in order of how badly a regression would hurt:

  1. A tenant_admin cannot read a report at all without a grant.
  2. The password step-up ticket is REQUIRED, single-use, and its failure
     mints nothing.
  3. A grant is scoped to ONE report — holding one for report A does not
     open report B.
  4. A break-glass read is distinguishable in the audit trail from a
     routine one.
"""

from __future__ import annotations

import contextlib
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from auth import Claims
from report_models import ReportStatus

ADMIN_SUB = UUID("11111111-1111-1111-1111-111111111111")
TENANT_ID = UUID("22222222-2222-2222-2222-222222222222")
REPORT_ID = UUID("33333333-3333-3333-3333-333333333333")
OTHER_REPORT_ID = UUID("3333333a-3333-3333-3333-333333333333")
VERSION_ID = UUID("55555555-5555-5555-5555-555555555555")
PATIENT_ID = UUID("88888888-8888-8888-8888-888888888888")
AUTHOR_SUB = UUID("99999999-9999-9999-9999-999999999999")
GRANT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

GOOD_TICKET = "a-valid-step-up-ticket"


def _claims(*roles: str, sub: UUID = ADMIN_SUB) -> Claims:
    return Claims(
        sub=sub,
        tid=TENANT_ID,
        roles=list(roles),
        sid="test-session",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
        preferred_username="admin@tenant-a.example",
    )


def _report_row(report_id: UUID = REPORT_ID):
    from report_service.domain.reports_repository import ReportRow

    return ReportRow(
        id=report_id,
        tenant_id=TENANT_ID,
        code="REP-2026-00001",
        status=ReportStatus.FINALIZED,
        current_version_id=VERSION_ID,
        current_version_number=1,
        primary_author_id=AUTHOR_SUB,
        co_author_ids=[],
        title="Chest CT",
        icd10_codes=[],
        encounter_date=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        finalized_at=None,
        signed_at=None,
        cancelled_at=None,
        patient_id=PATIENT_ID,
        patient_name_redacted="І.П.",
    )


def _grant_record(*, resource_id: UUID = REPORT_ID, reason_code: str = "legal_request"):
    """A stand-in for the asyncpg.Record the repository returns."""
    now = datetime.now(UTC)
    return {
        "id": GRANT_ID,
        "tenant_id": TENANT_ID,
        "requested_by": ADMIN_SUB,
        "resource_kind": "report",
        "resource_id": resource_id,
        "patient_id": PATIENT_ID,
        "reason_code": reason_code,
        "reason_note": "Court order 12/2026",
        "status": "granted",
        "granted_at": now,
        "expires_at": now + timedelta(hours=1),
        "revoked_at": None,
        "revoked_by": None,
        "use_count": 0,
        "last_used_at": None,
        "created_at": now,
    }


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """App + stubs. Returns a namespace so tests can steer the doubles."""
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from report_service import deps
    from report_service.main import create_app
    from report_service.routers import _phi_access_guard, phi_access, reports

    audit_calls: list[dict] = []
    published: list[dict] = []

    async def _write_event(**kwargs):  # noqa: ANN003
        audit_calls.append(kwargs)

    deps.install_state(  # type: ignore[arg-type]
        SimpleNamespace(
            app_pool=object(),
            redis=object(),
            audit_writer=SimpleNamespace(write_event=_write_event),
        )
    )

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    for module in (phi_access, reports, _phi_access_guard):
        monkeypatch.setattr(module, "tenant_connection", _fake_tenant_conn)

    state = SimpleNamespace(
        live_grant=None,
        ticket_ok=True,
        consumed=[],
        created=[],
        use_stamps=[],
    )

    # ── repository doubles ────────────────────────────────────────
    async def _fetch_report(conn, *, report_id):  # noqa: ANN001
        return _report_row(report_id)

    monkeypatch.setattr(phi_access.repo, "fetch_report", _fetch_report)
    monkeypatch.setattr(reports.repo, "fetch_report", _fetch_report)

    async def _fetch_patient_label(conn, *, patient_id):  # noqa: ANN001
        from report_service.domain.reports_repository import PatientLabel

        if patient_id != PATIENT_ID:
            return None
        return PatientLabel(id=patient_id, name_uk="Іван", name_en="Ivan")

    monkeypatch.setattr(phi_access.repo, "fetch_patient_label", _fetch_patient_label)

    async def _fetch_version(conn, *, version_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(reports.repo, "fetch_version", _fetch_version)

    async def _consume(conn, *, subject_sub, ticket_hash, purpose):  # noqa: ANN001
        state.consumed.append((subject_sub, ticket_hash, purpose))
        return state.ticket_ok

    monkeypatch.setattr(phi_access.grants, "consume_reauth_ticket", _consume)

    async def _create_grant(conn, **kwargs):  # noqa: ANN001, ANN003
        state.created.append(kwargs)
        return _grant_record(resource_id=kwargs["resource_id"])

    monkeypatch.setattr(phi_access.grants, "create_grant", _create_grant)

    async def _find_live(conn, *, user_sub, resource_id, resource_kind="report"):  # noqa: ANN001
        grant = state.live_grant
        if (
            grant is None
            or grant["resource_id"] != resource_id
            or grant["resource_kind"] != resource_kind
        ):
            return None
        return grant

    monkeypatch.setattr(_phi_access_guard.grants, "find_live_grant", _find_live)

    async def _record_use(conn, *, grant_id):  # noqa: ANN001
        state.use_stamps.append(grant_id)

    monkeypatch.setattr(_phi_access_guard.grants, "record_grant_use", _record_use)

    async def _emit(redis, **kwargs):  # noqa: ANN001, ANN003
        published.append(kwargs)

    monkeypatch.setattr(phi_access, "emit_report_event", _emit)

    app = create_app()
    state.app = app
    state.deps = deps
    state.audit_calls = audit_calls
    state.published = published
    state.client = TestClient(app, raise_server_exceptions=True)
    return state


def _as(env, claims: Claims) -> TestClient:
    env.app.dependency_overrides[env.deps.current_user] = lambda: claims
    return env.client


# ── 1. The split itself ──────────────────────────────────────────────


def test_admin_cannot_read_a_report_without_a_grant(env) -> None:
    resp = _as(env, _claims("tenant_admin")).get(f"/v1/reports/{REPORT_ID}?purpose=audit")
    assert resp.status_code == 403
    body = resp.json()
    # The SPA keys the "Request access" CTA off this code + resource id.
    assert body["code"] == "phi_access_required"
    assert body["resource_id"] == str(REPORT_ID)
    assert body["can_request_access"] is True


def test_auditor_is_refused_and_is_not_offered_break_glass(env) -> None:
    """An auditor may read the GRANT LOG, never the reports themselves —
    so they must not be shown a door that would refuse them anyway."""
    resp = _as(env, _claims("auditor")).get(f"/v1/reports/{REPORT_ID}?purpose=audit")
    assert resp.status_code == 403
    assert resp.json()["can_request_access"] is False


def test_clinician_reads_normally_and_is_not_flagged_break_glass(env) -> None:
    resp = _as(env, _claims("clinician")).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    assert resp.status_code == 200
    viewed = [c for c in env.audit_calls if c["kind"] == "report.viewed_full"]
    assert viewed and viewed[-1]["payload"]["break_glass"] is False
    assert not [c for c in env.audit_calls if c["kind"] == "phi_access.used"]


def test_admin_who_is_also_a_clinician_keeps_clinical_access(env) -> None:
    """The matrix is over roles, not people. A practising doctor who also
    administers the tenant holds both roles and loses nothing."""
    resp = _as(env, _claims("tenant_admin", "clinician")).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    assert resp.status_code == 200


# ── 2. The step-up ticket ────────────────────────────────────────────


def test_request_without_a_valid_ticket_mints_nothing(env) -> None:
    env.ticket_ok = False
    resp = _as(env, _claims("tenant_admin")).post(
        "/v1/phi-access-requests",
        json={
            "resource_id": str(REPORT_ID),
            "reason_code": "legal_request",
            "reauth_ticket": "stale-or-forged",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "reauth_required"
    assert env.created == []  # no grant
    assert env.published == []  # and nobody was told about a non-event
    assert [c["kind"] for c in env.audit_calls] == ["phi_access.denied"]


def test_ticket_is_matched_on_hash_subject_and_purpose(env) -> None:
    _as(env, _claims("tenant_admin")).post(
        "/v1/phi-access-requests",
        json={
            "resource_id": str(REPORT_ID),
            "reason_code": "legal_request",
            "reauth_ticket": GOOD_TICKET,
        },
    )
    subject, ticket_hash, purpose = env.consumed[0]
    assert subject == ADMIN_SUB
    assert purpose == "phi_access_request"
    # The raw ticket is never sent to the database.
    assert ticket_hash == hashlib.sha256(GOOD_TICKET.encode()).digest()


def test_reason_other_requires_a_written_justification(env) -> None:
    resp = _as(env, _claims("tenant_admin")).post(
        "/v1/phi-access-requests",
        json={
            "resource_id": str(REPORT_ID),
            "reason_code": "other",
            "reason_note": "because",
            "reauth_ticket": GOOD_TICKET,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "reason_note_required"
    # Rejected BEFORE the ticket is spent, so the user need not retype
    # their password to fix a too-short note.
    assert env.consumed == []


def test_clinician_cannot_request_break_glass(env) -> None:
    """They already hold report.read — a grant would be meaningless, and
    the endpoint existing for them would muddy the oversight log."""
    resp = _as(env, _claims("clinician")).post(
        "/v1/phi-access-requests",
        json={
            "resource_id": str(REPORT_ID),
            "reason_code": "quality_review",
            "reauth_ticket": GOOD_TICKET,
        },
    )
    assert resp.status_code == 403


# ── 3. Grant scope ───────────────────────────────────────────────────


def test_grant_opens_the_requested_report(env) -> None:
    env.live_grant = _grant_record()
    resp = _as(env, _claims("tenant_admin")).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    assert resp.status_code == 200
    assert env.use_stamps == [GRANT_ID]  # the read was counted


def test_grant_does_not_open_a_different_report(env) -> None:
    env.live_grant = _grant_record(resource_id=REPORT_ID)
    resp = _as(env, _claims("tenant_admin")).get(
        f"/v1/reports/{OTHER_REPORT_ID}?purpose=audit&include_content=false"
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "phi_access_required"
    assert env.use_stamps == []


# ── 4. The trail ─────────────────────────────────────────────────────


def test_granting_audits_at_sec_and_notifies_the_authors(env) -> None:
    resp = _as(env, _claims("tenant_admin")).post(
        "/v1/phi-access-requests",
        json={
            "resource_id": str(REPORT_ID),
            "reason_code": "legal_request",
            "reason_note": "Court order 12/2026",
            "reauth_ticket": GOOD_TICKET,
        },
    )
    assert resp.status_code == 201

    granted = [c for c in env.audit_calls if c["kind"] == "phi_access.granted"]
    assert len(granted) == 1
    assert str(granted[0]["severity"]) == "sec"
    # The justification lives in the chain — that is where a compliance
    # review looks for it.
    assert granted[0]["payload"]["reason_note"] == "Court order 12/2026"

    assert len(env.published) == 1
    event = env.published[0]
    assert event["primary_author_id"] == AUTHOR_SUB
    # The notification carries the CODE, never the note (PHI boundary).
    assert event["extra_payload"]["reason_code"] == "legal_request"
    assert "reason_note" not in event["extra_payload"]


def test_break_glass_read_is_distinguishable_in_the_trail(env) -> None:
    env.live_grant = _grant_record()
    _as(env, _claims("tenant_admin")).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    kinds = [c["kind"] for c in env.audit_calls]
    assert "phi_access.used" in kinds

    viewed = [c for c in env.audit_calls if c["kind"] == "report.viewed_full"][0]
    assert viewed["payload"]["break_glass"] is True
    # Escalated from info: an admin reading a chart is not routine.
    assert str(viewed["severity"]) == "sec"

    used = [c for c in env.audit_calls if c["kind"] == "phi_access.used"][0]
    assert used["payload"]["grant_id"] == str(GRANT_ID)
    assert used["payload"]["reason_code"] == "legal_request"


def test_a_refused_attempt_is_itself_recorded(env) -> None:
    """"An admin keeps trying to open charts" must be visible."""
    _as(env, _claims("tenant_admin")).get(f"/v1/reports/{REPORT_ID}?purpose=audit")
    denied = [c for c in env.audit_calls if c["kind"] == "authz.denied"]
    assert len(denied) == 1
    assert denied[0]["payload"]["reason"] == "no_live_grant"
    assert str(denied[0]["severity"]) == "sec"


# ── 5. Patient-kind grants (S15) ─────────────────────────────────────


def test_patient_kind_grant_mints_and_tells_no_author(env) -> None:
    """A patient record has no author to notify — the after-the-fact
    control is the sec audit trail plus the oversight list only."""
    resp = _as(env, _claims("tenant_admin")).post(
        "/v1/phi-access-requests",
        json={
            "resource_kind": "patient",
            "resource_id": str(PATIENT_ID),
            "reason_code": "patient_complaint",
            "reauth_ticket": GOOD_TICKET,
        },
    )
    assert resp.status_code == 201, resp.text
    assert env.created[0]["resource_kind"] == "patient"
    assert env.created[0]["patient_id"] == PATIENT_ID

    granted = [c for c in env.audit_calls if c["kind"] == "phi_access.granted"]
    assert granted and granted[0]["target_kind"] == "patient"
    assert env.published == []  # no author, no notification


def test_patient_kind_grant_needs_an_existing_patient(env) -> None:
    """The lookup must fail BEFORE the ticket is spent — a typo must not
    burn the password step-up."""
    resp = _as(env, _claims("tenant_admin")).post(
        "/v1/phi-access-requests",
        json={
            "resource_kind": "patient",
            "resource_id": str(OTHER_REPORT_ID),  # no such patient
            "reason_code": "patient_complaint",
            "reauth_ticket": GOOD_TICKET,
        },
    )
    assert resp.status_code == 404
    assert env.consumed == []  # ticket untouched
    assert env.created == []


def test_a_patient_grant_does_not_open_a_report(env) -> None:
    """Kind isolation: a live grant on the PATIENT must not satisfy the
    REPORT guard, even for the id the report is about."""
    grant = _grant_record(resource_id=REPORT_ID)
    grant["resource_kind"] = "patient"
    env.live_grant = grant
    resp = _as(env, _claims("tenant_admin")).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit"
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "phi_access_required"
