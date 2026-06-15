"""GET /auth/me — verified claims + DB user record."""

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
            SELECT sub, tenant_id, email, display_name, role, status,
                   mfa_enrolled_at, last_login_at, created_at, updated_at
            FROM users
            WHERE sub = $1
            """,
            claims.sub,
        )
    db_user: dict[str, Any] | None = None
    if row is not None:
        db_user = {
            "sub": str(row["sub"]),
            "tenant_id": str(row["tenant_id"]),
            "email": row["email"],
            "display_name": row["display_name"],
            "role": row["role"],
            "status": row["status"],
            "mfa_enrolled_at": (
                row["mfa_enrolled_at"].isoformat() if row["mfa_enrolled_at"] else None
            ),
            "last_login_at": (row["last_login_at"].isoformat() if row["last_login_at"] else None),
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
