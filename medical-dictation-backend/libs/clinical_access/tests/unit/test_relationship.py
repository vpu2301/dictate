"""The predicate the whole break-glass control hangs off.

These run against a fake connection rather than Postgres: the SQL shape
is pinned by the DB-gated integration suite, and what matters here is the
DECISION — which principals resolve to a relationship and which do not.
That is the part a reviewer needs to read, and it must be runnable
without a database or it will not be run.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from clinical_access import (
    RelationshipBasis,
    has_report_relationship,
    has_treatment_relationship,
    relationship_with_patient,
    relationship_with_report,
)

PATIENT = UUID("0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0a")
AUTHOR = UUID("11111111-1111-1111-1111-111111111111")
CO_AUTHOR = UUID("22222222-2222-2222-2222-222222222222")
ENCOUNTER_DOC = UUID("33333333-3333-3333-3333-333333333333")
STRANGER_DOC = UUID("44444444-4444-4444-4444-444444444444")
ADMIN = UUID("55555555-5555-5555-5555-555555555555")
REPORT = UUID("66666666-6666-6666-6666-666666666666")
ORPHAN_REPORT = UUID("77777777-7777-7777-7777-777777777777")


class FakeConn:
    """Answers the two queries the module issues, from in-memory tables.

    Dispatches on a distinguishing fragment of each statement rather than
    on call order, so a future reordering inside the module does not
    silently turn these into tests of something else.
    """

    def __init__(
        self,
        *,
        reports: list[dict] | None = None,
        encounters: list[dict] | None = None,
    ) -> None:
        self.reports = reports or []
        self.encounters = encounters or []
        self.queries: list[str] = []

    async def fetchrow(self, sql: str, *args):
        self.queries.append(sql)
        if "FROM reports WHERE id" in sql:
            report_id = args[0]
            for r in self.reports:
                if r["id"] == report_id:
                    return {
                        "patient_id": r["patient_id"],
                        "primary_author_id": r["primary_author_id"],
                        "co_author_ids": r["co_author_ids"],
                    }
            return None

        # The UNION-ALL basis query.
        user_sub, patient_id = args
        if any(
            r["patient_id"] == patient_id and r["primary_author_id"] == user_sub
            for r in self.reports
        ):
            return {"basis": "author"}
        if any(
            r["patient_id"] == patient_id and user_sub in r["co_author_ids"]
            for r in self.reports
        ):
            return {"basis": "co_author"}
        if any(
            e["patient_id"] == patient_id and e["created_by"] == user_sub
            for e in self.encounters
        ):
            return {"basis": "encounter_clinician"}
        return None


def _conn() -> FakeConn:
    return FakeConn(
        reports=[
            {
                "id": REPORT,
                "patient_id": PATIENT,
                "primary_author_id": AUTHOR,
                "co_author_ids": [CO_AUTHOR],
            },
            {
                "id": ORPHAN_REPORT,
                "patient_id": None,
                "primary_author_id": AUTHOR,
                "co_author_ids": [],
            },
        ],
        encounters=[{"patient_id": PATIENT, "created_by": ENCOUNTER_DOC}],
    )


# ── The three ways in ───────────────────────────────────────────────────


async def test_primary_author_has_a_relationship():
    rel = await relationship_with_patient(_conn(), user_sub=AUTHOR, patient_id=PATIENT)
    assert rel.exists
    assert rel.basis is RelationshipBasis.AUTHOR


async def test_co_author_has_a_relationship():
    rel = await relationship_with_patient(
        _conn(), user_sub=CO_AUTHOR, patient_id=PATIENT
    )
    assert rel.exists
    assert rel.basis is RelationshipBasis.CO_AUTHOR


async def test_encounter_clinician_has_a_relationship():
    """Seeing the patient counts even with nothing written yet — otherwise
    the doctor mid-consultation is a stranger to their own patient."""
    rel = await relationship_with_patient(
        _conn(), user_sub=ENCOUNTER_DOC, patient_id=PATIENT
    )
    assert rel.exists
    assert rel.basis is RelationshipBasis.ENCOUNTER_CLINICIAN


# ── The two ways out — this is the defect closure ──────────────────────


async def test_unrelated_clinician_has_no_relationship():
    """A clinician who has never touched this patient. Same tenant, full
    `report.read`/`patient.read_full` — and still no relationship, so the
    read goes through break-glass."""
    rel = await relationship_with_patient(
        _conn(), user_sub=STRANGER_DOC, patient_id=PATIENT
    )
    assert not rel.exists
    assert rel.basis is RelationshipBasis.NONE
    assert bool(rel) is False


async def test_tenant_admin_has_no_relationship():
    """The snooping-admin case. Nothing about being an administrator can
    produce a relationship — the predicate never looks at roles."""
    assert (
        await has_treatment_relationship(_conn(), user_sub=ADMIN, patient_id=PATIENT)
    ) is False


@pytest.mark.parametrize(
    ("who", "expected"),
    [
        (AUTHOR, True),
        (CO_AUTHOR, True),
        (ENCOUNTER_DOC, True),
        (STRANGER_DOC, False),
        (ADMIN, False),
    ],
)
async def test_relationship_matrix(who, expected):
    """The whole table in one place — the slice pasted into the hotfix
    evidence."""
    assert (
        await has_treatment_relationship(_conn(), user_sub=who, patient_id=PATIENT)
    ) is expected


# ── Report-scoped variant ──────────────────────────────────────────────


async def test_report_relationship_follows_authorship():
    rel = await relationship_with_report(_conn(), user_sub=AUTHOR, report_id=REPORT)
    assert rel.basis is RelationshipBasis.AUTHOR


async def test_report_relationship_falls_through_to_the_patient():
    """The clinician who saw the patient, reading a note they did not
    write. Related — the relationship is with the person, not the file."""
    rel = await relationship_with_report(
        _conn(), user_sub=ENCOUNTER_DOC, report_id=REPORT
    )
    assert rel.basis is RelationshipBasis.ENCOUNTER_CLINICIAN


async def test_report_relationship_denies_a_stranger():
    assert (
        await has_report_relationship(_conn(), user_sub=STRANGER_DOC, report_id=REPORT)
    ) is False


async def test_patientless_report_is_author_only():
    """`reports.patient_id` is nullable. Authorship is then the only
    relationship available — and asking the patient clause about NULL
    would have wrongly called the author a stranger."""
    conn = _conn()
    assert (
        await has_report_relationship(conn, user_sub=AUTHOR, report_id=ORPHAN_REPORT)
    ) is True
    assert (
        await has_report_relationship(
            conn, user_sub=ENCOUNTER_DOC, report_id=ORPHAN_REPORT
        )
    ) is False


async def test_missing_report_is_not_a_relationship():
    """Invisible under RLS reads the same as non-existent — the caller
    turns it into its own 404 rather than the predicate guessing."""
    missing = UUID("99999999-9999-9999-9999-999999999999")
    assert (
        await has_report_relationship(_conn(), user_sub=AUTHOR, report_id=missing)
    ) is False


async def test_patientless_report_does_not_query_the_patient_clause():
    """Guards the short-circuit: a NULL patient_id must not reach the
    UNION query with a NULL argument."""
    conn = _conn()
    await has_report_relationship(conn, user_sub=STRANGER_DOC, report_id=ORPHAN_REPORT)
    assert len(conn.queries) == 1
    assert "FROM reports WHERE id" in conn.queries[0]
