"""The gate on reading one patient's record (S15).

The core-service twin of report-service's ``_phi_access_guard``. Two ways
through it, and the handler needs to know which one was used:

  * ``patient.read_full`` — a clinician or nurse. Ordinary clinical access.
  * a live break-glass grant — a tenant_admin who requested THIS patient,
    gave a reason, and re-entered their password. The grant is scoped to
    one patient and expires; holding one says nothing about any other.

Anything else is 403. The admin case answers with a machine-readable
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
    """

    claims: Claims
    grant_id: UUID | None = None
    reason_code: str | None = None

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
    if can_claims(claims, "patient.read_full", "patient"):
        return PatientAccess(claims=claims)

    # No standing clinical read. Break-glass is the only remaining door,
    # and only for roles that hold the roster + may request it at all.
    can_request = can_claims(claims, "patient.read", "patient") and can_claims(
        claims, "phi_access.request", "phi_access_request"
    )
    if not can_request:
        await _audit_denied(claims, patient_id, reason="role_denied")
        raise _forbidden(patient_id, can_request=False)

    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        grant = await grants.find_live_patient_grant(
            conn, user_sub=claims.sub, patient_id=patient_id
        )
        if grant is None:
            await _audit_denied(claims, patient_id, reason="no_live_grant")
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
