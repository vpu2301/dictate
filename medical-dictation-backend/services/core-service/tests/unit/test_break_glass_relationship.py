"""HOTFIX — the core-service half of both defects.

DEFECT 1 (consent signing): `POST /patients/{id}/consents/{cid}/sign`
gated on `patient.write`, which BOTH `nurse` and `tenant_admin` hold — so
an operational administrator could apply a КЕП to a clinical consent. It
now gates on `consent.sign`, clinician-only. Recording that a consent
exists stays under `patient.write`, because a nurse capturing a consent
is a real and legitimate act.

DEFECT 2 (break-glass): opening one patient's record gated on
`patient.read_full` alone, which every clinician and nurse holds — so
every chart in the clinic was open to every clinical user with no reason,
no step-up and no `sec` audit. It now additionally requires a treatment
relationship, and the unrelated caller joins the admin at the break-glass
door.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest
from clinical_access import NO_RELATIONSHIP, Relationship, RelationshipBasis
from fastapi.testclient import TestClient

PATIENT_ID = UUID("0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0a")
CONSENT_ID = UUID("0b0b0b0b-0b0b-0b0b-0b0b-0b0b0b0b0b0b")

NON_SIGNING_ROLES = ["nurse", "tenant_admin", "auditor", "service", "knowledge_admin"]


@pytest.fixture(autouse=True)
def _no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """No patient row and no consent row.

    Every assertion in this file is about the GUARD, so what the handler
    would have returned is irrelevant — but it must not be an exception,
    or "was I refused?" stops being a readable assertion. With these
    stubbed, passing the guard means a clean 404.
    """
    from core_service.domain import consents_repository, patients_repository

    async def _none(*args, **kwargs):
        return None

    monkeypatch.setattr(patients_repository, "get_patient", _none)
    monkeypatch.setattr(consents_repository, "get_consent", _none)


# ── DEFECT 1: consent signing is clinician-only ────────────────────────


@pytest.mark.parametrize("role", NON_SIGNING_ROLES)
def test_consent_signing_is_403_for_non_clinicians(
    make_client: Callable[[list[str]], TestClient], role: str
) -> None:
    resp = make_client([role]).post(
        f"/patients/{PATIENT_ID}/consents/{CONSENT_ID}/sign",
        json={"provider": "dev_password", "password": "dev-password"},
    )
    assert resp.status_code == 403, f"{role} reached the consent sign route"


def test_consent_signing_is_not_implied_by_patient_write() -> None:
    """The precise shape of the defect: `patient.write` is held by nurse
    AND tenant_admin, and gating the signature on it made both signers."""
    from auth.perms import can

    for role in ("nurse", "tenant_admin"):
        assert can(role, "patient.write", "patient") is True
        assert can(role, "consent.sign", "consent") is False
    assert can("clinician", "consent.sign", "consent") is True


def test_recording_a_consent_still_works_for_a_nurse(
    make_client: Callable[[list[str]], TestClient],
) -> None:
    """The fix must not take away the capture. A nurse witnessing and
    recording a consent is ordinary, correct ward work — only the
    qualified signature is withheld."""
    resp = make_client(["nurse"]).post(
        f"/patients/{PATIENT_ID}/consents",
        json={"type": "ai_scribe", "method": "verbal", "version": "v1"},
    )
    assert resp.status_code != 403


def test_clinician_passes_the_consent_sign_guard(
    make_client: Callable[[list[str]], TestClient],
) -> None:
    resp = make_client(["clinician"]).post(
        f"/patients/{PATIENT_ID}/consents/{CONSENT_ID}/sign",
        json={"provider": "dev_password", "password": "dev-password"},
    )
    assert resp.status_code != 403


# ── DEFECT 2: the relationship gate on patient reads ───────────────────


def test_related_clinician_reads_normally(
    make_client: Callable[[list[str]], TestClient],
) -> None:
    """The unchanged path. The default fixture relationship is AUTHOR."""
    client = make_client(["clinician"])
    resp = client.get(f"/patients/{PATIENT_ID}")
    assert resp.status_code != 403


@pytest.mark.parametrize("role", ["clinician", "nurse"])
def test_unrelated_clinical_role_reads_normally(
    make_client: Callable[[list[str]], TestClient], role: str
) -> None:
    """Reverted 2026-08-09: a clinical role opens the chart on its standing
    permission, related or not.

    For a fortnight this was a 403 with a break-glass offer, and it was the
    wrong instrument. Covering a colleague's shift, seeing a patient for the
    first time, being asked for a second opinion — none of these leaves a
    prior trace for the relationship predicate to match on, and every one of
    them was being made to write a justification to do the job. A control
    that fires on the normal case teaches people to click through it.

    The read is still RECORDED with its (absent) relationship — see
    `test_an_unrelated_clinical_read_is_still_recorded` — which is the
    difference between an unreviewable block and a reviewable fact.
    """
    client = make_client([role])
    client.relationship["value"] = NO_RELATIONSHIP
    resp = client.get(f"/patients/{PATIENT_ID}")
    assert resp.status_code != 403, "a clinical role is not challenged for a chart"


def test_admin_is_challenged(make_client: Callable[[list[str]], TestClient]) -> None:
    """The snooping-admin case. An administrator has no standing read at
    all, so the relationship never even gets consulted."""
    client = make_client(["tenant_admin"])
    client.relationship["value"] = NO_RELATIONSHIP
    resp = client.get(f"/patients/{PATIENT_ID}")
    assert resp.status_code == 403
    assert resp.json()["code"] == "phi_access_required"


def test_auditor_gets_role_denied_not_a_break_glass_offer(
    make_client: Callable[[list[str]], TestClient],
) -> None:
    """An auditor reads the grant log, never the charts. Offering them the
    request dialog would be a lie about what they can have."""
    client = make_client(["auditor"])
    client.relationship["value"] = NO_RELATIONSHIP
    resp = client.get(f"/patients/{PATIENT_ID}")
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "role_denied"
    assert body["can_request_access"] is False


@pytest.mark.parametrize(
    "basis",
    [
        RelationshipBasis.AUTHOR,
        RelationshipBasis.CO_AUTHOR,
        RelationshipBasis.ENCOUNTER_CLINICIAN,
    ],
)
def test_every_relationship_basis_opens_the_record(
    make_client: Callable[[list[str]], TestClient], basis: RelationshipBasis
) -> None:
    """All three ways of being on a patient's care are equally sufficient
    — a co-author is not a second-class reader of their own report."""
    client = make_client(["clinician"])
    client.relationship["value"] = Relationship(basis=basis)
    resp = client.get(f"/patients/{PATIENT_ID}")
    assert resp.status_code != 403


def test_a_refused_read_is_audited_at_sec_severity(
    make_client: Callable[[list[str]], TestClient],
) -> None:
    """"An admin keeps trying to open charts" only becomes visible if the
    refusals are recorded, not just the successes."""
    from audit import Severity

    client = make_client(["tenant_admin"])
    client.relationship["value"] = NO_RELATIONSHIP
    client.get(f"/patients/{PATIENT_ID}")

    denied = [c for c in client.audit_calls if c["kind"] == "authz.denied"]
    assert denied, "a refused patient read wrote no audit event"
    event = denied[-1]
    assert event["severity"] is Severity.SEC
    assert event["payload"]["action"] == "patient.read_full"
    assert str(event["target_id"]) == str(PATIENT_ID)


def test_an_unrelated_clinical_read_is_not_a_denial(
    make_client: Callable[[list[str]], TestClient],
) -> None:
    """The counterpart to the revert: no relationship is no longer a refusal,
    so it must no longer be audited as one.

    An `authz.denied` event for a read that succeeded would poison exactly the
    review this trail exists to support — a compliance officer filtering for
    denials would find a page of reads that were, in fact, allowed.
    """
    client = make_client(["clinician"])
    client.relationship["value"] = NO_RELATIONSHIP
    resp = client.get(f"/patients/{PATIENT_ID}")

    assert resp.status_code != 403
    denied = [c for c in client.audit_calls if c["kind"] == "authz.denied"]
    assert denied == [], "an allowed read must not be recorded as a denial"


def test_the_admin_refusal_still_names_its_cause(
    make_client: Callable[[list[str]], TestClient],
) -> None:
    """The refusal that DOES remain is the one worth filtering on: an
    administrator with no standing clinical read and no live grant."""
    client = make_client(["tenant_admin"])
    client.relationship["value"] = NO_RELATIONSHIP
    client.get(f"/patients/{PATIENT_ID}")

    denied = [c for c in client.audit_calls if c["kind"] == "authz.denied"]
    assert denied
    assert denied[-1]["payload"]["reason"] == "no_live_grant"
