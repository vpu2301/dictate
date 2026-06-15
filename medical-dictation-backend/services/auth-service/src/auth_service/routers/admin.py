"""POST /admin/users/invite, POST /admin/users/{sub}/deactivate.

These are tenant_admin-only operations. Day 7 wires the formal
``requires(action="user.invite", target_kind="user")`` matrix; for Day 6
we gate inline on the `tenant_admin` role.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from ..deps import get_state, requires, requires_mfa
from ..keycloak_client import KeycloakError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

_ROLE_VALUES: frozenset[str] = frozenset({"tenant_admin", "clinician", "nurse", "auditor"})


class InviteRequest(BaseModel):
    # Basic email shape — we don't enforce reserved-TLD rules here so that
    # @example.test / @e2e.test work in integration tests. Production
    # tenants can layer additional validation upstream if needed.
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    display_name: str = Field(min_length=1, max_length=200)
    role: str
    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)


class InviteResponse(BaseModel):
    sub: str
    email: str
    role: str
    status: str


@router.post(
    "/users/invite",
    response_model=InviteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Invite a new user; creates the Keycloak user and the DB row",
    # MFA gate fires only when MDX_REQUIRE_MFA=true (off in sprint-02 pilot).
    dependencies=[Depends(requires_mfa())],
)
async def invite_user(
    body: InviteRequest,
    claims: Annotated[Claims, Depends(requires("user.invite", "user"))],
) -> InviteResponse:
    if body.role not in _ROLE_VALUES:
        raise HTTPException(status_code=422, detail=f"role must be one of {sorted(_ROLE_VALUES)}")

    state = get_state()
    tenant_id = claims.tid

    # 1. Create the Keycloak user (admin API). Returns the new sub.
    try:
        sub = await state.keycloak.create_user(
            email=body.email,
            first_name=body.first_name or body.display_name.split()[0],
            last_name=body.last_name or " ".join(body.display_name.split()[1:]) or "User",
            tenant_id=tenant_id,
            realm_role=body.role,
        )
    except KeycloakError as exc:
        if exc.status == 409:
            raise HTTPException(status_code=409, detail="email already registered") from exc
        raise HTTPException(status_code=502, detail=f"keycloak error: {exc}") from exc

    # 2. Mirror in the local users table (tenant_writer-scoped).
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO users (sub, tenant_id, email, display_name, role, status)
            VALUES ($1, $2, $3, $4, $5, 'invited')
            """,
            sub,
            tenant_id,
            body.email,
            body.display_name,
            body.role,
        )

    # 3. Audit (severity info — invite is a routine admin action).
    await state.audit_writer.write_event(
        tenant_id=tenant_id,
        kind=audit_kinds.USER_INVITED,
        actor_sub=claims.sub,
        actor_role="tenant_admin",
        target_kind="user",
        target_id=str(sub),
        payload={"email": body.email, "role": body.role},
        severity=Severity.INFO,
    )

    return InviteResponse(sub=str(sub), email=body.email, role=body.role, status="invited")


@router.post(
    "/users/{sub}/deactivate",
    status_code=status.HTTP_200_OK,
    summary="Soft-deactivate a user and revoke their sessions",
    dependencies=[Depends(requires_mfa())],
)
async def deactivate_user(
    sub: UUID,
    claims: Annotated[Claims, Depends(requires("user.deactivate", "user"))],
) -> dict[str, Any]:
    state = get_state()
    tenant_id = claims.tid

    # 1. Verify the target user belongs to the caller's tenant (RLS will
    #    refuse to return it otherwise, but we want an explicit 404).
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        existing = await conn.fetchrow("SELECT sub, status FROM users WHERE sub = $1", sub)
    if existing is None:
        raise HTTPException(status_code=404, detail="user not found in this tenant")

    # 2. Flip status in the DB.
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        await conn.execute("UPDATE users SET status = 'deactivated' WHERE sub = $1", sub)

    # 3. Disable + revoke sessions in Keycloak.
    try:
        await state.keycloak.set_user_enabled(sub, enabled=False)
        await state.keycloak.logout_user(sub)
    except KeycloakError as exc:
        # The local DB change has already committed; log and continue.
        logger.warning(
            "admin.deactivate.kc_partial_failure",
            extra={"sub": str(sub), "kc_status": exc.status, "body": exc.body},
        )

    # 4. Audit (severity sec — deactivation is sensitive).
    await state.audit_writer.write_event(
        tenant_id=tenant_id,
        kind=audit_kinds.USER_DEACTIVATED,
        actor_sub=claims.sub,
        actor_role="tenant_admin",
        target_kind="user",
        target_id=str(sub),
        payload={"prev_status": existing["status"]},
        severity=Severity.SEC,
    )

    return {"sub": str(sub), "status": "deactivated"}
