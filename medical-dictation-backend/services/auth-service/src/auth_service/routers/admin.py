"""POST /admin/users/invite, POST /admin/users/{sub}/deactivate.

These are tenant_admin-only operations. Day 7 wires the formal
``requires(action="user.invite", target_kind="user")`` matrix; for Day 6
we gate inline on the `tenant_admin` role.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from audit import Severity
from auth import Claims
from auth.perms import KNOWN_ROLES
from db import tenant_connection

from .. import audit_kinds
from ..deps import get_state, requires, requires_mfa
from ..keycloak_client import KeycloakError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

_ROLE_VALUES: frozenset[str] = frozenset({"tenant_admin", "clinician", "nurse", "auditor"})

# Highest-privilege-wins order used to collapse a multi-role set into the
# single ``users.role`` column (the table stores one role; Keycloak holds the
# full set). Keep in sync with the realm role catalogue.
_ROLE_PRECEDENCE: tuple[str, ...] = ("tenant_admin", "clinician", "nurse", "auditor", "service")


def _primary_role(roles: set[str]) -> str:
    """Collapse a role set to the single value the ``users.role`` column holds."""
    for role in _ROLE_PRECEDENCE:
        if role in roles:
            return role
    return sorted(roles)[0]


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


# ── Read surface (list / get) ────────────────────────────────────────────


class UserSummary(BaseModel):
    sub: str
    email: str
    display_name: str
    role: str
    status: str


class UserDetail(UserSummary):
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_login_at: datetime | None = None
    mfa_enrolled_at: datetime | None = None


@router.get(
    "/users",
    response_model=list[UserSummary],
    summary="List users in the caller's tenant (RLS-scoped, paginated)",
)
async def list_users(
    claims: Annotated[Claims, Depends(requires("user.read", "user"))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[UserSummary]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await conn.fetch(
            """
            SELECT sub, email, display_name, role, status
            FROM users
            ORDER BY created_at DESC, email
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
    return [
        UserSummary(
            sub=str(r["sub"]),
            email=r["email"],
            display_name=r["display_name"],
            role=r["role"],
            status=r["status"],
        )
        for r in rows
    ]


@router.get(
    "/users/{sub}",
    response_model=UserDetail,
    summary="Read one user in the caller's tenant",
)
async def get_user(
    sub: UUID,
    claims: Annotated[Claims, Depends(requires("user.read", "user"))],
) -> UserDetail:
    state = get_state()
    # RLS restricts visibility to the caller's tenant; a cross-tenant sub is
    # simply invisible → 404 (we do not leak its existence).
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await conn.fetchrow(
            """
            SELECT sub, email, display_name, role, status,
                   created_at, updated_at, last_login_at, mfa_enrolled_at
            FROM users
            WHERE sub = $1
            """,
            sub,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="user not found in this tenant")
    return UserDetail(
        sub=str(row["sub"]),
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_login_at=row["last_login_at"],
        mfa_enrolled_at=row["mfa_enrolled_at"],
    )


# ── Reactivate (mirror of deactivate) ────────────────────────────────────


@router.post(
    "/users/{sub}/reactivate",
    status_code=status.HTTP_200_OK,
    summary="Reactivate a previously deactivated user",
    dependencies=[Depends(requires_mfa())],
)
async def reactivate_user(
    sub: UUID,
    claims: Annotated[Claims, Depends(requires("user.reactivate", "user"))],
) -> dict[str, Any]:
    state = get_state()
    tenant_id = claims.tid

    async with tenant_connection(state.app_pool, tenant_id) as conn:
        existing = await conn.fetchrow("SELECT sub, status FROM users WHERE sub = $1", sub)
    if existing is None:
        raise HTTPException(status_code=404, detail="user not found in this tenant")

    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        await conn.execute(
            "UPDATE users SET status = 'active', updated_at = now() WHERE sub = $1", sub
        )

    # Re-enable the Keycloak account so the user can log in again.
    try:
        await state.keycloak.set_user_enabled(sub, enabled=True)
    except KeycloakError as exc:
        logger.warning(
            "admin.reactivate.kc_partial_failure",
            extra={"sub": str(sub), "kc_status": exc.status, "body": exc.body},
        )

    # Audit (severity sec — re-granting access is sensitive).
    await state.audit_writer.write_event(
        tenant_id=tenant_id,
        kind=audit_kinds.USER_REACTIVATED,
        actor_sub=claims.sub,
        actor_role="tenant_admin",
        target_kind="user",
        target_id=str(sub),
        payload={"prev_status": existing["status"]},
        severity=Severity.SEC,
    )

    return {"sub": str(sub), "status": "active"}


# ── Role management (the sprint-02 deferred endpoint) ─────────────────────


class RolesRequest(BaseModel):
    roles: list[str] = Field(min_length=1)


class RolesResponse(BaseModel):
    sub: str
    roles: list[str]


@router.put(
    "/users/{sub}/roles",
    response_model=RolesResponse,
    summary="Set a user's realm roles (tenant_admin only)",
    dependencies=[Depends(requires_mfa())],
)
async def set_user_roles(
    sub: UUID,
    body: RolesRequest,
    claims: Annotated[Claims, Depends(requires("user.manage_roles", "user"))],
) -> RolesResponse:
    # 1. Validate the requested roles against the known realm-role catalogue.
    desired = sorted(set(body.roles))
    unknown = [r for r in desired if r not in KNOWN_ROLES]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown role(s) {unknown}; must be a subset of {sorted(KNOWN_ROLES)}",
        )

    state = get_state()
    tenant_id = claims.tid
    desired_set = set(desired)

    # 2. The target must exist in the caller's tenant (RLS → 404 otherwise).
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        existing = await conn.fetchrow("SELECT sub, role FROM users WHERE sub = $1", sub)
    if existing is None:
        raise HTTPException(status_code=404, detail="user not found in this tenant")

    # 3. Read the current realm roles (old set, for the audit + last-admin guard).
    try:
        old_all = await state.keycloak.get_realm_roles(sub)
    except KeycloakError as exc:
        raise HTTPException(status_code=502, detail=f"keycloak error: {exc}") from exc
    old_app_roles = sorted(set(old_all) & KNOWN_ROLES)

    # 4. Guardrail: never strip the last tenant_admin of a tenant.
    if "tenant_admin" in old_app_roles and "tenant_admin" not in desired_set:
        async with tenant_connection(state.app_pool, tenant_id) as conn:
            n_admins = await conn.fetchval(
                "SELECT count(*) FROM users "
                "WHERE role = 'tenant_admin' AND status <> 'deactivated'"
            )
        if n_admins is not None and n_admins <= 1:
            raise HTTPException(
                status_code=409,
                detail="cannot remove the last tenant_admin of the tenant",
            )

    # 5. Apply the role change in Keycloak (managed = our app roles only).
    try:
        await state.keycloak.set_realm_roles(sub, desired=desired, managed=KNOWN_ROLES)
    except KeycloakError as exc:
        raise HTTPException(status_code=502, detail=f"keycloak error: {exc}") from exc

    # 6. Mirror the collapsed primary role in the local users table.
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        await conn.execute(
            "UPDATE users SET role = $2, updated_at = now() WHERE sub = $1",
            sub,
            _primary_role(desired_set),
        )

    # 7. Audit (severity sec — role changes are security-relevant; old → new).
    await state.audit_writer.write_event(
        tenant_id=tenant_id,
        kind=audit_kinds.USER_ROLE_CHANGED,
        actor_sub=claims.sub,
        actor_role="tenant_admin",
        target_kind="user",
        target_id=str(sub),
        payload={"old_roles": old_app_roles, "new_roles": desired},
        severity=Severity.SEC,
    )

    return RolesResponse(sub=str(sub), roles=desired)
