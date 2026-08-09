"""Is this principal on this patient's care?

One module, one definition, because the whole break-glass control hangs
off this predicate. If report-service and core-service each grew their
own answer they would drift, and the drift would show up as a chart
opening in one surface and challenging in another — which teaches staff
that the challenge is noise.

    A principal has a TREATMENT RELATIONSHIP with a patient when they are

      (a) the primary author or a co-author of any report belonging to
          that patient, or
      (b) the clinician who opened any of that patient's encounters.

Everything else — every tenant_admin, and every clinician who has never
touched this patient — has no relationship, and reaches the record
through break-glass: a reason, a fresh step-up, a `sec` audit row.

Deliberately NOT in the definition:

  * **Role.** A clinician with no history on this patient is as unrelated
    as an administrator is. The role decides what you may do once you are
    in; the relationship decides whether you walk in or break glass.
  * **Tenant membership.** RLS already scopes every query below to the
    caller's tenant, so "same clinic" is a precondition, never a
    relationship.
  * **Having read it before.** Circular: a break-glass read would mint
    the relationship that makes the next read routine.

An explicit care-team table is the natural third clause and there is a
seam for it in :func:`has_treatment_relationship` — the pilot has no such
table, and inventing one would mean inventing the workflow that
maintains it. Authorship and encounters are what the schema actually
records about who is looking after whom.

Every function here takes an already-open, RLS-scoped connection
(``db.tenant_connection``). The lib does no connection management of its
own, which is what keeps it a leaf: it never learns about pools,
settings, or claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import asyncpg


class RelationshipBasis(StrEnum):
    """WHY the predicate answered yes.

    Carried into the audit payload. "Granted because they authored the
    report" and "granted because they saw the patient in March" are
    different facts, and a reviewer asking whether the relationship model
    is behaving needs to tell them apart.
    """

    AUTHOR = "author"
    CO_AUTHOR = "co_author"
    ENCOUNTER_CLINICIAN = "encounter_clinician"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Relationship:
    """The predicate's answer plus its reason."""

    basis: RelationshipBasis

    @property
    def exists(self) -> bool:
        return self.basis is not RelationshipBasis.NONE

    def __bool__(self) -> bool:
        return self.exists


NO_RELATIONSHIP: Relationship = Relationship(basis=RelationshipBasis.NONE)


async def relationship_with_patient(
    conn: asyncpg.Connection,
    *,
    user_sub: UUID,
    patient_id: UUID,
) -> Relationship:
    """The full answer for a patient-scoped read.

    A single round trip: the authorship and encounter clauses are UNIONed
    server-side rather than run as two awaits, because this sits in front
    of every patient read in the product and a second round trip is a
    second chance to be slow on the hot path.

    Ordering is by specificity — authorship outranks co-authorship
    outranks having opened an encounter — so the basis reported is the
    strongest one that holds, not whichever the planner found first.
    """
    row = await conn.fetchrow(
        """
        SELECT basis FROM (
            -- Each branch is PARENTHESISED because it carries its own LIMIT.
            -- Postgres parses a bare `... LIMIT 1 UNION ALL ...` as a LIMIT on
            -- the whole set operation and rejects it — "syntax error at or
            -- near UNION" — which took this predicate, and therefore EVERY
            -- patient read in the product, to a 500.
            (SELECT 'author'::text AS basis, 1 AS rank
               FROM reports
              WHERE patient_id = $2 AND primary_author_id = $1
              LIMIT 1)
            UNION ALL
            (SELECT 'co_author'::text, 2
               FROM reports
              WHERE patient_id = $2 AND $1 = ANY (co_author_ids)
              LIMIT 1)
            UNION ALL
            (SELECT 'encounter_clinician'::text, 3
               FROM encounters
              WHERE patient_id = $2 AND created_by = $1
              LIMIT 1)
        ) AS bases
        ORDER BY rank
        LIMIT 1
        """,
        user_sub,
        patient_id,
    )
    if row is None:
        return NO_RELATIONSHIP
    return Relationship(basis=RelationshipBasis(row["basis"]))


async def has_treatment_relationship(
    conn: asyncpg.Connection,
    *,
    user_sub: UUID,
    patient_id: UUID,
) -> bool:
    """Boolean form of :func:`relationship_with_patient`."""
    return (
        await relationship_with_patient(
            conn, user_sub=user_sub, patient_id=patient_id
        )
    ).exists


async def relationship_with_report(
    conn: asyncpg.Connection,
    *,
    user_sub: UUID,
    report_id: UUID,
) -> Relationship:
    """The report-scoped variant.

    Authorship of THIS report is checked directly rather than through the
    patient clause, which matters for the case the patient clause cannot
    express: a report with no patient linked (``patient_id IS NULL`` —
    the schema allows it). For those, authorship is the only relationship
    there is, and asking about the patient would dereference nothing and
    wrongly answer "unrelated" to the person who wrote the document.

    When the report IS linked to a patient, a relationship with the
    patient carries: the clinician who saw them in March is not a
    stranger to the note written in April. That is the point of modelling
    the relationship against the patient rather than the document.
    """
    row = await conn.fetchrow(
        "SELECT patient_id, primary_author_id, co_author_ids "
        "FROM reports WHERE id = $1",
        report_id,
    )
    if row is None:
        # Non-existent, or invisible under this tenant's RLS. Either way
        # there is nothing to have a relationship with; the caller turns
        # this into its own 404.
        return NO_RELATIONSHIP

    if row["primary_author_id"] == user_sub:
        return Relationship(basis=RelationshipBasis.AUTHOR)
    if user_sub in (row["co_author_ids"] or ()):
        return Relationship(basis=RelationshipBasis.CO_AUTHOR)

    patient_id: UUID | None = row["patient_id"]
    if patient_id is None:
        return NO_RELATIONSHIP
    return await relationship_with_patient(
        conn, user_sub=user_sub, patient_id=patient_id
    )


async def has_report_relationship(
    conn: asyncpg.Connection,
    *,
    user_sub: UUID,
    report_id: UUID,
) -> bool:
    """Boolean form of :func:`relationship_with_report`."""
    return (
        await relationship_with_report(conn, user_sub=user_sub, report_id=report_id)
    ).exists
