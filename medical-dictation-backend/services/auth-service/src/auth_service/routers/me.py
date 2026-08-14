"""GET /auth/me — verified claims + DB user record.

S21 adds `db_user.mfa_reminder`: the open access-review request that this
user enrols a second factor. The SPA carries it as an undismissable banner
until `mfa_enrolled_at` is set, so this endpoint is what makes the reminder
"stand" rather than flash past as a notification.

Reading it here crosses no boundary — the subject learning that they were
asked to secure their own account is the whole point — but note what is NOT
returned: WHO asked. Only their role. An access-review finding is between
the reviewer and the audit log; naming them turns a security ask into an
interpersonal one.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from auth import Claims
from db import tenant_connection

from ..deps import current_user, get_state

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", summary="Return verified claims + DB user state")
async def me(claims: Annotated[Claims, Depends(current_user)]) -> dict[str, Any]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await conn.fetchrow(
            """
            SELECT u.sub, u.tenant_id, u.email, u.display_name, u.role, u.status,
                   u.mfa_enrolled_at, u.last_login_at, u.created_at, u.updated_at,
                   r.requested_by_role, r.first_reminded_at, r.last_reminded_at,
                   r.reminder_count
            FROM users u
            LEFT JOIN mfa_reminders r
                   ON r.tenant_id = u.tenant_id
                  AND r.subject_sub = u.sub
                  AND r.resolved_at IS NULL
            WHERE u.sub = $1
            """,
            claims.sub,
        )
    db_user: dict[str, Any] | None = None
    if row is not None:
        enrolled_at = row["mfa_enrolled_at"]
        # Belt and braces on the join's `resolved_at IS NULL`: if enrolment
        # ever lands without the resolve (the UPDATE pair in mfa.py is
        # best-effort against a DB hiccup), an enrolled user must still not
        # be told to go and enrol.
        reminder: dict[str, Any] | None = None
        if row["last_reminded_at"] is not None and enrolled_at is None:
            reminder = {
                "requested_by_role": row["requested_by_role"],
                "first_reminded_at": row["first_reminded_at"].isoformat(),
                "last_reminded_at": row["last_reminded_at"].isoformat(),
                "reminder_count": row["reminder_count"],
            }
        db_user = {
            "sub": str(row["sub"]),
            "tenant_id": str(row["tenant_id"]),
            "email": row["email"],
            "display_name": row["display_name"],
            "role": row["role"],
            "status": row["status"],
            "mfa_enrolled_at": (enrolled_at.isoformat() if enrolled_at else None),
            "last_login_at": (row["last_login_at"].isoformat() if row["last_login_at"] else None),
            "mfa_reminder": reminder,
        }
    return {
        "claims": {
            "sub": str(claims.sub),
            "tid": str(claims.tid),
            "roles": claims.roles,
            "scope": claims.scope,
            "mfa": claims.mfa,
            "iss": claims.iss,
        },
        "db_user": db_user,
    }
