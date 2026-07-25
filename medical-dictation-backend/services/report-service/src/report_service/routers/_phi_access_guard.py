"""The gate on reading one report's clinical content (S14).

Two ways through it, and the handler needs to know which one was used:

  * ``report.read`` — a clinician or nurse. Ordinary clinical access.
  * a live break-glass grant — a tenant_admin who requested THIS report,
    gave a reason, and re-entered their password. The grant is scoped to
    one report and expires; holding one says nothing about any other.

Anything else is 403. The admin case answers with a machine-readable
``phi_access_required`` code plus the report id, so the SPA can open the
request modal on the very report the user just tried to open rather than
dead-ending on a generic "forbidden".

Lives in the router layer, not ``deps``, because it reads the grants
table: ``routers → domain`` is the sanctioned direction (import-linter),
and a dependency that queried the database from ``deps`` would invert it.
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
class ReportReadAccess:
    """How the caller got in, so the handler can audit it honestly.

    ``grant_id is None`` means ordinary clinical access. A non-None value
    means this read happened under break-glass and every audit event the
    handler writes must carry it — a break-glass read that looks like a
    routine one in the trail defeats the entire control.
    """

    claims: Claims
    grant_id: UUID | None = None
    reason_code: str | None = None

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
    if can_claims(claims, "report.read", "report"):
        return ReportReadAccess(claims=claims)

    # No standing clinical read. Break-glass is the only remaining door,
    # and only for roles that may request it at all.
    can_request = can_claims(claims, "phi_access.request", "phi_access_request")
    if not can_request:
        await _audit_denied(claims, report_id, reason="role_denied")
        raise _forbidden(report_id, can_request=False)

    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        grant = await grants.find_live_grant(
            conn, user_sub=claims.sub, resource_id=report_id
        )
        if grant is None:
            await _audit_denied(claims, report_id, reason="no_live_grant")
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
