"""S15 patient break-glass — the admin ⟂ PHI split over the patient record.

Exercises the real handlers with the auth dependency overridden and the
DB/audit boundary stubbed, mirroring report-service's
``test_phi_access_break_glass``.

What these pin, in order of how badly a regression would hurt:

  1. A tenant_admin sees only a REDACTED roster (name + id) and cannot
     open a patient's demographics, timeline, visit history or anamnesis
     without a grant.
  2. A grant is scoped to ONE patient — holding one for patient A does
     not open patient B.
  3. A break-glass read is distinguishable in the audit trail from a
     routine clinical one, and every read is counted against its grant.
  4. Clinicians and nurses are entirely unaffected.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from audit import Severity
from tests.conftest import REQUESTER_SUB, TENANT_ID

PATIENT_ID = UUID("33333333-3333-3333-3333-333333333333")
OTHER_PATIENT_ID = UUID("3333333a-3333-3333-3333-333333333333")
GRANT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _patient_row(**over: object) -> dict:
    base: dict = {
        "id": PATIENT_ID,
        "tenant_id": TENANT_ID,
        "name_uk": "Іван Петренко",
        "name_en": "Ivan Petrenko",
        "dob": date(1980, 1, 15),
        "sex": "M",
        "mrn": "MRN-1",
        "phone": "+380671234567",
        "email": "ivan@example.com",
        "address_street": "вул. Шевченка",
        "address_house": "12, кв. 5",
        "address_zip": "01001",
        "address_city": "Київ",
        "address_country": "Україна",
        "summary_uk": "",
        "summary_en": "",
        "tags": ["diabetes"],
        "status": "active",
        "last_visit_at": NOW,
        "created_by": REQUESTER_SUB,
        "created_at": NOW,
        "updated_at": NOW,
        "has_ipn": True,
    }
    base.update(over)
    return base


def _grant(patient_id: UUID = PATIENT_ID) -> dict:
    return {
        "id": GRANT_ID,
        "resource_kind": "patient",
        "resource_id": patient_id,
        "reason_code": "patient_complaint",
    }


@pytest.fixture
def env(make_client, monkeypatch: pytest.MonkeyPatch):
    """Guard + repository doubles on top of the shared ``make_client``."""
    from core_service.domain import patients_repository, timeline_repository
    from core_service.routers import _phi_access_guard

    state = SimpleNamespace(live_grant=None, use_stamps=[])

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(_phi_access_guard, "tenant_connection", _fake_tenant_conn)

    async def _find_live(conn, *, user_sub, patient_id):  # noqa: ANN001
        grant = state.live_grant
        if grant is None or grant["resource_id"] != patient_id:
            return None
        return grant

    monkeypatch.setattr(
        _phi_access_guard.grants, "find_live_patient_grant", _find_live
    )

    async def _record_use(conn, *, grant_id):  # noqa: ANN001
        state.use_stamps.append(grant_id)

    monkeypatch.setattr(_phi_access_guard.grants, "record_grant_use", _record_use)

    async def _get_patient(conn, *, patient_id):  # noqa: ANN001
        if patient_id in (PATIENT_ID, OTHER_PATIENT_ID):
            return _patient_row(id=patient_id)
        return None

    monkeypatch.setattr(patients_repository, "get_patient", _get_patient)

    async def _list_patients(conn, **kwargs):  # noqa: ANN001, ANN003
        return [_patient_row()]

    monkeypatch.setattr(patients_repository, "list_patients", _list_patients)

    async def _update_patient(conn, *, patient_id, fields):  # noqa: ANN001
        return _patient_row(id=patient_id, **fields)

    monkeypatch.setattr(patients_repository, "update_patient", _update_patient)

    async def _empty(conn, *, patient_id):  # noqa: ANN001
        return []

    monkeypatch.setattr(timeline_repository, "list_patient_reports", _empty)
    monkeypatch.setattr(timeline_repository, "list_patient_recordings", _empty)
    monkeypatch.setattr(timeline_repository, "list_patient_conversations", _empty)

    state.make_client = make_client
    return state


def _client(env, *roles: str) -> TestClient:
    return env.make_client(list(roles))


# ── 1. The redacted roster ───────────────────────────────────────────


def test_admin_roster_is_redacted_to_name_and_id(env) -> None:
    resp = _client(env, "tenant_admin").get("/patients")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["id"] == str(PATIENT_ID)
    assert item["name"]["uk"] == "Іван Петренко"
    # Everything clinical waits behind the grant.
    assert item["dob"] is None
    assert item["sex"] == "U"
    assert item["mrn"] == ""
    assert item["phone"] == ""
    assert item["email"] == ""
    assert item["address"]["city"] == ""
    assert item["tags"] == []
    assert item["last_visit"] is None
    assert item["has_ipn"] is False


def test_clinician_roster_is_full(env) -> None:
    resp = _client(env, "clinician").get("/patients")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["mrn"] == "MRN-1"
    assert item["dob"] == "1980-01-15"
    assert item["phone"] == "+380671234567"


# ── 2. The gate on one patient ───────────────────────────────────────


def test_admin_cannot_open_a_patient_without_a_grant(env) -> None:
    resp = _client(env, "tenant_admin").get(f"/patients/{PATIENT_ID}")
    assert resp.status_code == 403
    body = resp.json()
    # The SPA keys the "Request access" CTA off this code + resource id.
    assert body["code"] == "phi_access_required"
    assert body["resource_kind"] == "patient"
    assert body["resource_id"] == str(PATIENT_ID)
    assert body["can_request_access"] is True


def test_denied_attempt_is_audited_at_sec(env) -> None:
    client = _client(env, "tenant_admin")
    client.get(f"/patients/{PATIENT_ID}")
    denied = [c for c in client.audit_calls if c["kind"] == "authz.denied"]
    assert denied and denied[-1]["severity"] == Severity.SEC
    assert denied[-1]["payload"]["reason"] == "no_live_grant"


def test_auditor_is_refused_and_is_not_offered_break_glass(env) -> None:
    resp = _client(env, "auditor").get(f"/patients/{PATIENT_ID}")
    assert resp.status_code == 403
    assert resp.json()["can_request_access"] is False


def test_admin_with_live_grant_reads_and_is_flagged(env) -> None:
    env.live_grant = _grant()
    client = _client(env, "tenant_admin")
    resp = client.get(f"/patients/{PATIENT_ID}")
    assert resp.status_code == 200
    assert resp.json()["mrn"] == "MRN-1"  # full record, not the redaction
    assert env.use_stamps == [GRANT_ID]
    viewed = [c for c in client.audit_calls if c["kind"] == "patient.viewed"]
    assert viewed and viewed[-1]["payload"]["break_glass"] is True
    assert viewed[-1]["severity"] == Severity.SEC
    used = [c for c in client.audit_calls if c["kind"] == "phi_access.used"]
    assert used and used[-1]["payload"]["grant_id"] == str(GRANT_ID)
    assert used[-1]["payload"]["surface"] == "patient_detail"


def test_grant_is_scoped_to_one_patient(env) -> None:
    env.live_grant = _grant(OTHER_PATIENT_ID)
    resp = _client(env, "tenant_admin").get(f"/patients/{PATIENT_ID}")
    assert resp.status_code == 403


def test_clinician_reads_normally_and_is_not_flagged_break_glass(env) -> None:
    client = _client(env, "clinician")
    resp = client.get(f"/patients/{PATIENT_ID}")
    assert resp.status_code == 200
    viewed = [c for c in client.audit_calls if c["kind"] == "patient.viewed"]
    assert viewed and viewed[-1]["payload"]["break_glass"] is False
    assert viewed[-1]["severity"] == Severity.INFO
    assert not [c for c in client.audit_calls if c["kind"] == "phi_access.used"]


def test_admin_who_is_also_a_clinician_keeps_clinical_access(env) -> None:
    """The matrix is over roles, not people — a practising doctor who
    also administers the tenant loses nothing."""
    resp = _client(env, "tenant_admin", "clinician").get(f"/patients/{PATIENT_ID}")
    assert resp.status_code == 200


# ── 3. The other record surfaces ride the same gate ──────────────────


def test_timeline_is_gated_and_counted(env) -> None:
    client = _client(env, "tenant_admin")
    assert client.get(f"/patients/{PATIENT_ID}/timeline").status_code == 403

    env.live_grant = _grant()
    resp = client.get(f"/patients/{PATIENT_ID}/timeline")
    assert resp.status_code == 200
    used = [c for c in client.audit_calls if c["kind"] == "phi_access.used"]
    assert used and used[-1]["payload"]["surface"] == "patient_timeline"


def test_admin_update_requires_a_grant(env) -> None:
    client = _client(env, "tenant_admin")
    resp = client.put(f"/patients/{PATIENT_ID}", json={"mrn": "MRN-9"})
    assert resp.status_code == 403

    env.live_grant = _grant()
    resp = client.put(f"/patients/{PATIENT_ID}", json={"mrn": "MRN-9"})
    assert resp.status_code == 200
    updated = [c for c in client.audit_calls if c["kind"] == "patient.updated"]
    assert updated and updated[-1]["payload"]["break_glass"] is True
    assert updated[-1]["severity"] == Severity.SEC


def test_anamnesis_and_visit_history_are_gated(env) -> None:
    client = _client(env, "tenant_admin")
    assert client.get(f"/patients/{PATIENT_ID}/anamnesis").status_code == 403
    assert client.get(f"/patients/{PATIENT_ID}/encounters").status_code == 403


def test_clinician_update_is_untouched(env) -> None:
    client = _client(env, "clinician")
    resp = client.put(f"/patients/{PATIENT_ID}", json={"mrn": "MRN-9"})
    assert resp.status_code == 200
    updated = [c for c in client.audit_calls if c["kind"] == "patient.updated"]
    assert updated and updated[-1]["payload"]["break_glass"] is False
