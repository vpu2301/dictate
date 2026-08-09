"""The gate on reading one patient's record (S15 + hotfix).

The core-service twin of report-service's ``_phi_access_guard``, and it
must stay a twin: two definitions of who may open a chart is one
definition too many. Both call the same predicate in
``libs/clinical_access``. Two ways through, and the handler needs to
know which one was used:

  * ``patient.read_full`` — a clinician or nurse. Ordinary clinical
    access. The treatment relationship is resolved and recorded for the
    audit trail, but it is NOT a condition: covering a colleague, seeing
    a patient for the first time, or being asked for a second opinion are
    all normal, and none of them leaves a prior trace to match on.
  * a live break-glass grant — anyone else who requested THIS patient,
    gave a reason, and passed a fresh step-up. The grant is scoped to
    one patient and expires; holding one says nothing about any other.

Standing clinical permission answers "may you open charts at all"; the
relationship answers "is this patient already yours", and the answer is
written into the audit event rather than used to refuse. Clinical reads
are therefore visible and reviewable but never blocked — the boundary
this guard exists to hold is the ADMIN one, and that is unchanged: an
administrator holds no ``patient.read_full`` and reaches a chart only
through a per-patient, expiring, reasoned break-glass grant.

Anything else is 403. The refusal answers with a machine-readable
``phi_access_required`` code plus the patient id, so the SPA can open the
request modal on the very patient the user just tried to open rather
than dead-ending on a generic "forbidden".

The roster LIST is deliberately NOT behind this guard: an admin keeps a
redacted roster (name + id — see ``list_patients``), because a door you
cannot find the handle of is a wall.

Lives in the router layer, not ``deps``, because it reads the grants
table: ``routers → domain`` is the sanctioned direction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from clinical_access import RelationshipBasis, relationship_with_patient
from fastapi import Depends, HTTPException, status

from audit import Severity
from auth import Claims, can_claims
from db import tenant_connection

from ..deps import current_user, get_state
from ..domain import phi_access_repository as grants

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PatientAccess:
    """How the caller got in, so the handler can audit it honestly.

    ``grant_id is None`` means ordinary clinical access. A non-None value
    means this read happened under break-glass and the audit events the
    handler writes must carry it — a break-glass read that looks like a
    routine one in the trail defeats the entire control.

    ``relationship`` records WHY the ordinary path was open (author,
    co-author, encounter clinician); on the break-glass path it is
    ``NONE`` by construction — that is what made it break-glass.
    """

    claims: Claims
    grant_id: UUID | None = None
    reason_code: str | None = None
    relationship: RelationshipBasis = RelationshipBasis.NONE

    @property
    def is_break_glass(self) -> bool:
        return self.grant_id is not None


def _forbidden(patient_id: UUID, *, can_request: bool) -> HTTPException:
    exc = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "opening this patient record requires an approved break-glass request"
            if can_request
            else "your role does not permit reading patient records"
        ),
    )
    exc.problem_extras = {  # type: ignore[attr-defined]
        "code": "phi_access_required" if can_request else "role_denied",
        "resource_kind": "patient",
        "resource_id": str(patient_id),
        "can_request_access": can_request,
    }
    return exc


async def patient_record_access(
    patient_id: UUID,
    claims: Annotated[Claims, Depends(current_user)],
) -> PatientAccess:
    """Dependency for every endpoint that serves one patient's record.

    Also guards the WRITE path (``PUT /patients/{id}``): editing a record
    presumes reading it, so an admin's edit rides the same grant. The
    write endpoints keep their separate ``patient.write`` check.
    """
    state = get_state()
    has_standing_read = can_claims(claims, "patient.read_full", "patient")
    # Break-glass needs the roster too: without `patient.read` the caller
    # could not have found this id legitimately in the first place.
    can_request = can_claims(claims, "patient.read", "patient") and can_claims(
        claims, "phi_access.request", "phi_access_request"
    )

    # Neither door. An auditor or service token is done here, with no
    # database work.
    if not has_standing_read and not can_request:
        await _audit_denied(claims, patient_id, reason="role_denied")
        raise _forbidden(patient_id, can_request=False)

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        relationship = (
            await relationship_with_patient(
                conn, user_sub=claims.sub, patient_id=patient_id
            )
            if has_standing_read
            else None
        )
        if relationship is not None:
            # Ordinary clinical access. Standing clinical permission IS the
            # decision (2026-08-09): a clinician or nurse holding
            # `patient.read_full` opens the record, related or not.
            #
            # The relationship predicate was briefly a GATE here, and it was
            # the wrong instrument. A clinic is not a set of private caseloads:
            # the doctor covering a colleague's shift, the nurse preparing the
            # room, the second opinion asked for in the corridor — none of them
            # has authored a report or opened an encounter yet, and every one
            # of them was being told to break glass to do their job. A control
            # that fires on the normal case is not a control, it is an
            # obstacle, and the first thing it teaches is to click through the
            # justification box without reading it.
            #
            # So the basis is RECORDED, not required: it rides into the audit
            # payload (`relationship`), where "this clinician had no prior
            # connection to this patient" stays a visible, reviewable fact —
            # which is what the trail is for. The hard boundary remains where
            # it belongs: an ADMIN holds no `patient.read_full` at all and
            # still cannot pass this point without a live break-glass grant.
            return PatientAccess(claims=claims, relationship=relationship.basis)

        # No standing read at all — an admin. Break-glass or nothing.
        if not can_request:
            await _audit_denied(claims, patient_id, reason="no_relationship")
            raise _forbidden(patient_id, can_request=False)

        grant = await grants.find_live_patient_grant(
            conn, user_sub=claims.sub, patient_id=patient_id
        )
        if grant is None:
            await _audit_denied(
                claims,
                patient_id,
                reason="no_live_grant" if not has_standing_read else "no_relationship",
            )
            raise _forbidden(patient_id, can_request=True)
        # Stamp the use inside the same RLS-scoped connection that
        # authorised it, so a read can never be served without also
        # being counted.
        await grants.record_grant_use(conn, grant_id=grant["id"])

    return PatientAccess(
        claims=claims,
        grant_id=grant["id"],
        reason_code=grant["reason_code"],
    )


async def _audit_denied(claims: Claims, patient_id: UUID, *, reason: str) -> None:
    """A refused attempt on a patient record is itself security-relevant —
    it is how "an admin keeps trying to open charts" becomes visible."""
    state = get_state()
    try:
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind="authz.denied",
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="patient",
            target_id=str(patient_id),
            payload={"action": "patient.read_full", "reason": reason},
            severity=Severity.SEC,
        )
    except Exception as exc:
        logger.warning("phi_access.denied_audit_failed", extra={"error": str(exc)})
