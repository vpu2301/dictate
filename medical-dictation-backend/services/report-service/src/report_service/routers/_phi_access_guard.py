"""The gate on reading one report's clinical content (S14 + hotfix).

Two ways through it, and the handler needs to know which one was used:

  * ``report.read`` — a clinician or nurse. Ordinary clinical access. The
    treatment relationship is resolved and RECORDED for the audit trail,
    but it is not a condition of entry.
  * a live break-glass grant — anyone else who requested THIS report,
    gave a reason, and passed a fresh step-up. The grant is scoped to
    one report and expires; holding one says nothing about any other.

Anything else is 403, answered with a machine-readable
``phi_access_required`` code plus the report id, so the SPA can open the
request modal on the very report the user just tried to open rather than
dead-ending on a generic "forbidden".

── What the hotfix changed ────────────────────────────────────────────

S14 made the first door ``report.read`` alone. That permission is held
by every clinician and nurse in the tenant, so it opened every patient's
record to every clinical user — the sprint-08 read-purpose gate asked
non-authors for a ``?purpose=`` string, but a string is not a control:
no closed vocabulary, no step-up, no `sec` audit, nothing to review.
An admin was walled off from the clinical record while a clinician with
no connection to the patient walked straight in.

…and for a fortnight the fix was to require BOTH, which sent the
unrelated clinician to the break-glass door alongside the admin. That
was the wrong instrument (2026-08-09). A clinic is not a set of private
caseloads: covering a colleague's shift, seeing a patient for the first
time, being asked for a second opinion — none of these leaves a prior
trace to match on, and every one of them was being made to write a
justification to do the job. A control that fires on the normal case
teaches people to click through it.

So standing clinical permission is the decision, and the relationship is
written into the audit event instead — "this clinician had no prior
connection to this patient" stays a visible, reviewable fact rather than
a refusal. The hard boundary is unchanged and is the one that matters:
an ADMIN holds no ``report.read`` at all and still reaches a report only
through a per-report, expiring, reasoned break-glass grant.

Lives in the router layer, not ``deps``, because it reads the grants
table: ``routers → domain`` is the sanctioned direction (import-linter),
and a dependency that queried the database from ``deps`` would invert it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from clinical_access import RelationshipBasis, relationship_with_report
from fastapi import Depends, HTTPException, status

from audit import Severity
from auth import Claims, can_claims
from db import tenant_connection

from ..deps import current_user, get_state
from ..domain import phi_access_repository as grants

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReportReadAccess:
    """How the caller got in, so the handler can audit it honestly.

    ``grant_id is None`` means ordinary clinical access. A non-None value
    means this read happened under break-glass and every audit event the
    handler writes must carry it — a break-glass read that looks like a
    routine one in the trail defeats the entire control.

    ``relationship`` records WHY the ordinary path was open (author,
    co-author, encounter clinician). On the break-glass path it is
    ``NONE`` by construction — that is what made it break-glass.
    """

    claims: Claims
    grant_id: UUID | None = None
    reason_code: str | None = None
    relationship: RelationshipBasis = RelationshipBasis.NONE

    @property
    def is_break_glass(self) -> bool:
        return self.grant_id is not None


def _forbidden(report_id: UUID, *, can_request: bool) -> HTTPException:
    exc = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "reading this report requires an approved break-glass request"
            if can_request
            else "your role does not permit reading clinical reports"
        ),
    )
    exc.problem_extras = {  # type: ignore[attr-defined]
        "code": "phi_access_required" if can_request else "role_denied",
        "resource_kind": "report",
        "resource_id": str(report_id),
        "can_request_access": can_request,
    }
    return exc


async def report_read_access(
    report_id: UUID,
    claims: Annotated[Claims, Depends(current_user)],
) -> ReportReadAccess:
    """Dependency for every endpoint that serves a single report's content."""
    state = get_state()
    has_standing_read = can_claims(claims, "report.read", "report")
    can_request = can_claims(claims, "phi_access.request", "phi_access_request")

    # A role that can neither read reports nor break glass is done here,
    # with no database work: an auditor or a service token has nothing to
    # look up. Kept ahead of the connection so the cheapest refusal is
    # also the fastest.
    if not has_standing_read and not can_request:
        await _audit_denied(claims, report_id, reason="role_denied")
        raise _forbidden(report_id, can_request=False)

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        # Both remaining paths need the relationship, and both need the
        # same RLS-scoped connection, so it is resolved once here.
        relationship = (
            await relationship_with_report(
                conn, user_sub=claims.sub, report_id=report_id
            )
            if has_standing_read
            else None
        )
        if relationship is not None:
            # Ordinary clinical access. Standing clinical permission IS the
            # decision (2026-08-09) — see the module docstring, and the twin
            # in core-service, for why the relationship stopped being a gate
            # and became an audit fact. The sprint-08 read-purpose rules on
            # the handler are unchanged.
            return ReportReadAccess(claims=claims, relationship=relationship.basis)

        # No standing read — an admin. Break-glass or nothing. Still refused
        # identically when they cannot request one, so a role that somehow
        # lost `phi_access.request` cannot fall back to open access.
        if not can_request:
            await _audit_denied(claims, report_id, reason="no_relationship")
            raise _forbidden(report_id, can_request=False)

        grant = await grants.find_live_grant(
            conn, user_sub=claims.sub, resource_id=report_id, resource_kind="report"
        )
        if grant is None:
            await _audit_denied(
                claims,
                report_id,
                reason="no_live_grant" if not has_standing_read else "no_relationship",
            )
            raise _forbidden(report_id, can_request=True)
        # Stamp the use inside the same RLS-scoped connection that
        # authorised it, so a read can never be served without also being
        # counted.
        await grants.record_grant_use(conn, grant_id=grant["id"])

    return ReportReadAccess(
        claims=claims,
        grant_id=grant["id"],
        reason_code=grant["reason_code"],
    )


async def _audit_denied(claims: Claims, report_id: UUID, *, reason: str) -> None:
    """A refused attempt on a clinical record is itself security-relevant —
    it is how "an admin keeps trying to open charts" becomes visible."""
    state = get_state()
    try:
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind="authz.denied",
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="report",
            target_id=report_id,
            payload={"action": "report.read", "reason": reason},
            severity=Severity.SEC,
        )
    except Exception as exc:
        logger.warning("phi_access.denied_audit_failed", extra={"error": str(exc)})
