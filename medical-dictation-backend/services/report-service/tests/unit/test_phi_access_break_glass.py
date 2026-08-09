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
        # Subs treated as having seen this patient at an encounter. The
        # predicate itself is tested in libs/clinical_access; here it is
        # stubbed over the same in-memory report row the rest of the
        # fixture serves, so the two cannot disagree about who authored
        # what.
        encounter_clinicians=set(),
        # Co-authors of THIS report, steerable per-test.
        report_co_authors=[],
        all_grants=[],
    )

    async def _relationship_with_report(conn, *, user_sub, report_id):  # noqa: ANN001
        from clinical_access import (
            NO_RELATIONSHIP,
            Relationship,
            RelationshipBasis,
        )

        row = _report_row(report_id)
        if user_sub == row.primary_author_id:
            return Relationship(basis=RelationshipBasis.AUTHOR)
        if user_sub in list(row.co_author_ids) + state.report_co_authors:
            return Relationship(basis=RelationshipBasis.CO_AUTHOR)
        if user_sub in state.encounter_clinicians:
            return Relationship(basis=RelationshipBasis.ENCOUNTER_CLINICIAN)
        return NO_RELATIONSHIP

    monkeypatch.setattr(
        _phi_access_guard, "relationship_with_report", _relationship_with_report
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

    # The oversight surface. Filters are applied here rather than in SQL so
    # the test can assert the handler passes them through — the real
    # filtering is a WHERE clause and is exercised by the DB-gated suite.
    async def _list_grants(conn, *, requested_by=None, resource_id=None, active_only=False, limit=50):  # noqa: ANN001
        rows = list(state.all_grants)
        if requested_by is not None:
            rows = [r for r in rows if r["requested_by"] == requested_by]
        if resource_id is not None:
            rows = [r for r in rows if r["resource_id"] == resource_id]
        if active_only:
            rows = [r for r in rows if r["status"] == "granted"]
        return rows[:limit]

    monkeypatch.setattr(phi_access.grants, "list_grants", _list_grants)

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


def test_authoring_clinician_reads_normally_and_is_not_flagged_break_glass(env) -> None:
    """The unchanged path. The clinician who wrote it opens it exactly as
    before the hotfix: no reason, no step-up, `report.viewed_full` with
    `break_glass: False`, and no grant consumed."""
    resp = _as(env, _claims("clinician", sub=AUTHOR_SUB)).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    assert resp.status_code == 200
    viewed = [c for c in env.audit_calls if c["kind"] == "report.viewed_full"]
    assert viewed and viewed[-1]["payload"]["break_glass"] is False
    assert not [c for c in env.audit_calls if c["kind"] == "phi_access.used"]


def test_admin_who_is_also_a_clinician_keeps_clinical_access(env) -> None:
    """The matrix is over roles, not people. A practising doctor who also
    administers the tenant holds both roles and loses nothing — on their
    OWN patients. The relationship, not the admin role, is what opens it."""
    resp = _as(env, _claims("tenant_admin", "clinician", sub=AUTHOR_SUB)).get(
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


def test_clinician_may_not_request_break_glass(env) -> None:
    """Reverted 2026-08-09. `report.read` is once again the whole answer for
    a clinical role — the treatment relationship is audited, not required —
    so there is no door left for a clinician to walk through here, and a
    capability that mints grants must not sit on the role that cannot spend
    it. The admin keeps it, because for an admin it is the only way in."""
    resp = _as(env, _claims("clinician")).post(
        "/v1/phi-access-requests",
        json={
            "resource_id": str(REPORT_ID),
            "reason_code": "emergency_care",
            "reauth_ticket": GOOD_TICKET,
        },
    )
    assert resp.status_code == 403


def test_service_token_cannot_request_break_glass(env) -> None:
    """Break-glass is a human act with a human justification — a machine
    identity has no standing to make one."""
    resp = _as(env, _claims("service")).post(
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


# ── 5. HOTFIX — the treatment relationship ───────────────────────────
#
# Before the hotfix `report.read` alone opened any report in the tenant to
# any clinician or nurse: no reason, no step-up, and an `info`-level view
# event. The admin was walled off while the clinical staff were not. These
# pin the closure and, just as importantly, pin that the AUTHOR's ordinary
# day is untouched.


def test_unrelated_clinician_reads_normally(env) -> None:
    """Reverted 2026-08-09: a clinician with `report.read` opens the report,
    related to the patient or not. The relationship rides into the audit
    event instead of standing in the doorway — see the module guard for why
    a control that fires on the covering shift is not a control."""
    resp = _as(env, _claims("clinician")).get(  # ADMIN_SUB — not the author
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    assert resp.status_code == 200
    viewed = [c for c in env.audit_calls if c["kind"] == "report.viewed_full"]
    assert viewed and viewed[-1]["payload"]["break_glass"] is False


def test_unrelated_nurse_reads_normally(env) -> None:
    resp = _as(env, _claims("nurse")).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    assert resp.status_code == 200


def test_co_author_reads_normally(env) -> None:
    """A co-author is not a second-class reader of their own report."""
    co = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    env.report_co_authors.append(co)
    resp = _as(env, _claims("clinician", sub=co)).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    assert resp.status_code == 200
    viewed = [c for c in env.audit_calls if c["kind"] == "report.viewed_full"]
    assert viewed and viewed[-1]["payload"]["break_glass"] is False


def test_encounter_clinician_reads_a_note_they_did_not_write(env) -> None:
    """The relationship is with the PATIENT, not the document. The doctor
    who saw them in March is not a stranger to the note written in April."""
    doc = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    env.encounter_clinicians.add(doc)
    resp = _as(env, _claims("clinician", sub=doc)).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    assert resp.status_code == 200


def test_an_admin_with_a_grant_gets_in_and_is_flagged(env) -> None:
    """The full branch: reason + step-up mints a grant, the grant opens
    the report, and the read is recorded AS break-glass rather than as a
    routine view. A break-glass read that looks routine in the trail
    defeats the whole control."""
    env.live_grant = _grant_record(reason_code="emergency_care")
    resp = _as(env, _claims("tenant_admin")).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    assert resp.status_code == 200
    assert env.use_stamps == [GRANT_ID]  # the read was counted
    viewed = [c for c in env.audit_calls if c["kind"] == "report.viewed_full"]
    assert viewed and viewed[-1]["payload"]["break_glass"] is True
    assert [c for c in env.audit_calls if c["kind"] == "phi_access.used"]


def test_a_refused_admin_read_is_audited_at_sec(env) -> None:
    """The refusal that remains after the 2026-08-09 revert: an administrator
    with no standing clinical read and no live grant. A clinical read is no
    longer refused, so it is no longer audited as a denial either — an
    `authz.denied` row for a read that succeeded would poison the very review
    this trail exists to support."""
    from audit import Severity

    _as(env, _claims("tenant_admin")).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    denied = [c for c in env.audit_calls if c["kind"] == "authz.denied"]
    assert denied, "a refused report read wrote no audit event"
    assert denied[-1]["severity"] is Severity.SEC


def test_admin_refusal_still_says_no_live_grant(env) -> None:
    """An admin has no standing read at all, so the relationship is never
    consulted — the cause must stay distinguishable from a clinician's."""
    _as(env, _claims("tenant_admin")).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    denied = [c for c in env.audit_calls if c["kind"] == "authz.denied"]
    assert denied and denied[-1]["payload"]["reason"] == "no_live_grant"


def test_auditor_is_refused_without_a_break_glass_offer(env) -> None:
    """Offering an auditor the request dialog would be a lie about what
    they can have. They read the grant log, never the reports."""
    resp = _as(env, _claims("auditor")).get(
        f"/v1/reports/{REPORT_ID}?purpose=audit&include_content=false"
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "role_denied"
    assert resp.json()["can_request_access"] is False


def test_the_clinical_reason_codes_are_offered(env) -> None:
    """The vocabulary a clinician needs actually reaches the dropdown —
    without them every clinical break-glass would land as `other` and the
    oversight log would say nothing."""
    resp = _as(env, _claims("tenant_admin")).get("/v1/phi-access-requests/reasons")
    assert resp.status_code == 200
    codes = {r["code"] for r in resp.json()["reasons"]}
    assert {
        "emergency_care",
        "care_coordination",
        "patient_request",
        "technical_support",
    } <= codes
    # …and the S14 administrative vocabulary is still there.
    assert {"legal_request", "quality_review", "other"} <= codes


def test_technical_support_requires_a_written_justification(env) -> None:
    """"Support" covers everything from a rendering bug to reading a whole
    chart. The difference has to be written down."""
    resp = _as(env, _claims("tenant_admin")).post(
        "/v1/phi-access-requests",
        json={
            "resource_id": str(REPORT_ID),
            "reason_code": "technical_support",
            "reason_note": "bug",
            "reauth_ticket": GOOD_TICKET,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "reason_note_required"
    assert env.consumed == []  # rejected before the ticket is spent


def test_emergency_care_needs_no_note(env) -> None:
    """Demanding prose from someone mid-resuscitation is how a control
    gets routed around."""
    resp = _as(env, _claims("tenant_admin")).post(
        "/v1/phi-access-requests",
        json={
            "resource_id": str(REPORT_ID),
            "reason_code": "emergency_care",
            "reauth_ticket": GOOD_TICKET,
        },
    )
    assert resp.status_code == 201


# ── 6. HOTFIX — the review surface ───────────────────────────────────
#
# Break-glass that nobody can review is theatre. This is the surface a
# compliance officer actually opens.


def test_oversight_lists_break_glass_events(env) -> None:
    env.all_grants = [
        _grant_record(reason_code="emergency_care"),
        _grant_record(resource_id=OTHER_REPORT_ID, reason_code="legal_request"),
    ]
    resp = _as(env, _claims("tenant_admin")).get("/v1/phi-access-requests?limit=100")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    # Everything a review needs to judge the act: who, what, why, and how
    # hard they leaned on it.
    row = items[0]
    assert row["requested_by"] == str(ADMIN_SUB)
    assert row["reason_code"] == "emergency_care"
    assert row["reason_note"]
    assert row["patient_id"] == str(PATIENT_ID)
    assert "use_count" in row and "expires_at" in row


def test_oversight_filters_by_principal(env) -> None:
    """The snooping question — "what has THIS person been opening?" — is
    the one the burst alert points a reviewer at."""
    other = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    mine = _grant_record()
    theirs = _grant_record(resource_id=OTHER_REPORT_ID)
    theirs["requested_by"] = other
    env.all_grants = [mine, theirs]

    resp = _as(env, _claims("tenant_admin")).get(
        f"/v1/phi-access-requests?requested_by={other}"
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["requested_by"] == str(other)


def test_oversight_filters_to_live_grants(env) -> None:
    """"Who can read what right now" is a different question from "who
    ever could", and an incident needs the first one."""
    live = _grant_record()
    revoked = _grant_record(resource_id=OTHER_REPORT_ID)
    revoked["status"] = "revoked"
    env.all_grants = [live, revoked]

    resp = _as(env, _claims("tenant_admin")).get(
        "/v1/phi-access-requests?active_only=true"
    )
    assert len(resp.json()["items"]) == 1


def test_auditor_can_review(env) -> None:
    """The grant log is exactly what a compliance review needs, and it is
    the one PHI-adjacent surface an auditor holds."""
    env.all_grants = [_grant_record()]
    resp = _as(env, _claims("auditor")).get("/v1/phi-access-requests")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1


@pytest.mark.parametrize("role", ["clinician", "nurse", "service"])
def test_oversight_is_not_open_to_everyone(env, role: str) -> None:
    """Being able to break glass does not make you an overseer of it."""
    resp = _as(env, _claims(role)).get("/v1/phi-access-requests")
    assert resp.status_code == 403
