"""Tenant (clinic) management — the backend for the SPA "Tenant" sidebar.

Endpoints
---------
* ``GET    /tenants``                       — tenants the caller can reach
* ``GET    /tenants/current``               — the caller's active tenant
* ``POST   /tenants``                       — onboard a new clinic (→ owner)
* ``GET    /tenants/{id}``                   — tenant profile / branding
* ``PATCH  /tenants/{id}``                   — update profile / branding
* ``PUT    /tenants/{id}/logo``              — upload / replace the logo
* ``GET    /tenants/{id}/logo``              — fetch the logo bytes
* ``GET    /tenants/{id}/members``           — the member roster
* ``POST   /tenants/{id}/members``           — link a principal (+role)
* ``PATCH  /tenants/{id}/members/{sub}``      — change a member's role
* ``DELETE /tenants/{id}/members/{sub}``      — remove a member
* ``POST   /tenants/{id}/switch``            — select active tenant

Isolation model
---------------
Reads of a tenant require the caller to hold a membership in that tenant
(cross-tenant reads allowed for tenants you belong to). *Writes* (create is
the exception) additionally require the target to be the caller's **active**
tenant — the JWT is scoped to one tenant, so you manage the tenant you are
authenticated into. The perms matrix (``tenant.read`` / ``tenant.update`` /
``tenant.create`` / ``tenant.manage_members``) is the platform-role gate;
membership presence + management role is the per-tenant gate on top.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from .. import tenants_repository as repo
from ..deps import current_user, get_state, requires, requires_mfa

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tenants", tags=["tenants"])

# Management roles carried by a membership (distinct from the JWT platform
# roles). The ones that may administer the tenant.
MANAGEMENT_ROLES: frozenset[str] = frozenset(
    {"owner", "admin", "doctor", "nurse", "assistant", "viewer"}
)
MANAGER_ROLES: frozenset[str] = frozenset({"owner", "admin"})

_MAX_LOGO_BYTES = 2 * 1024 * 1024
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


# ── Wire models (extra="forbid" per platform rule 10) ───────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TenantOut(_Strict):
    id: UUID
    name: str
    display_name: str
    legal_name: str
    slug: str | None
    locale: str
    timezone: str
    status: str
    is_active: bool
    logo_url: str
    has_logo: bool = False
    contact_email: str
    phone_number: str
    website: str
    address_line1: str
    address_line2: str
    postal_code: str
    city: str
    state_or_region: str
    country: str
    tax_id: str
    registration_number: str
    created_at: datetime
    updated_at: datetime
    my_role: str | None = None


class TenantSummary(_Strict):
    id: UUID
    name: str
    display_name: str
    slug: str | None
    status: str
    is_active: bool
    logo_url: str
    my_role: str


class TenantListOut(_Strict):
    items: list[TenantSummary]


class TenantCreate(_Strict):
    name: str = Field(min_length=2, max_length=120)
    display_name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=120)
    legal_name: str = Field(default="", max_length=200)
    locale: str = Field(default="uk", max_length=16)
    timezone: str = Field(default="Europe/Kyiv", max_length=64)
    contact_email: str = Field(default="", max_length=320)
    phone_number: str = Field(default="", max_length=64)
    website: str = Field(default="", max_length=320)
    address_line1: str = Field(default="", max_length=200)
    address_line2: str = Field(default="", max_length=200)
    postal_code: str = Field(default="", max_length=32)
    city: str = Field(default="", max_length=120)
    state_or_region: str = Field(default="", max_length=120)
    country: str = Field(default="", max_length=120)
    tax_id: str = Field(default="", max_length=64)
    registration_number: str = Field(default="", max_length=64)


class TenantUpdate(_Strict):
    display_name: str | None = Field(default=None, max_length=200)
    legal_name: str | None = Field(default=None, max_length=200)
    slug: str | None = Field(default=None, max_length=120)
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    logo_url: str | None = Field(default=None, max_length=320)
    contact_email: str | None = Field(default=None, max_length=320)
    phone_number: str | None = Field(default=None, max_length=64)
    website: str | None = Field(default=None, max_length=320)
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    postal_code: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=120)
    state_or_region: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)
    tax_id: str | None = Field(default=None, max_length=64)
    registration_number: str | None = Field(default=None, max_length=64)


class MemberOut(_Strict):
    user_sub: UUID
    role: str
    status: str
    email: str | None = None
    display_name: str | None = None
    platform_role: str | None = None
    created_at: datetime
    updated_at: datetime


class MemberListOut(_Strict):
    items: list[MemberOut]


class MemberAdd(_Strict):
    user_sub: UUID | None = None
    email: str | None = Field(default=None, max_length=320)
    role: str = "viewer"


class MemberRoleUpdate(_Strict):
    role: str


class SwitchOut(_Strict):
    active_tenant: TenantSummary
    note: str


# ── Serialization ────────────────────────────────────────────────────────


def _tenant_out(row: asyncpg.Record, *, my_role: str | None = None) -> TenantOut:
    return TenantOut(
        id=row["id"],
        name=row["name"],
        display_name=row["display_name"],
        legal_name=row["legal_name"],
        slug=row["slug"],
        locale=row["locale"],
        timezone=row["timezone"],
        status=row["status"],
        is_active=row["is_active"],
        logo_url=row["logo_url"],
        has_logo=bool(row["logo_content_type"]),
        contact_email=row["contact_email"],
        phone_number=row["phone_number"],
        website=row["website"],
        address_line1=row["address_line1"],
        address_line2=row["address_line2"],
        postal_code=row["postal_code"],
        city=row["city"],
        state_or_region=row["state_or_region"],
        country=row["country"],
        tax_id=row["tax_id"],
        registration_number=row["registration_number"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        my_role=my_role,
    )


def _summary(row: asyncpg.Record, my_role: str) -> TenantSummary:
    return TenantSummary(
        id=row["id"],
        name=row["name"],
        display_name=row["display_name"],
        slug=row["slug"],
        status=row["status"],
        is_active=row["is_active"],
        logo_url=row["logo_url"],
        my_role=my_role,
    )


def _member_out(row: asyncpg.Record) -> MemberOut:
    return MemberOut(
        user_sub=row["user_sub"],
        role=row["role"],
        status=row["status"],
        email=row["email"],
        display_name=row["display_name"],
        platform_role=row["platform_role"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Guards ───────────────────────────────────────────────────────────────


async def _my_membership_role(tenant_id: UUID, sub: UUID) -> str | None:
    """The caller's management role in ``tenant_id`` (via unrestricted writer
    pool so it works for any tenant the caller belongs to), or None."""
    state = get_state()
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        m = await repo.get_membership(conn, tenant_id=tenant_id, user_sub=sub)
    return m["role"] if m is not None else None


async def _require_member(tenant_id: UUID, claims: Claims) -> str:
    role = await _my_membership_role(tenant_id, claims.sub)
    if role is None:
        # Do not leak existence of a tenant the caller can't see.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")
    return role


def _require_active_tenant(tenant_id: UUID, claims: Claims) -> None:
    """Writes may only target the tenant the caller is authenticated into —
    the JWT is single-tenant, so RLS and Keycloak roles both refer to it."""
    if tenant_id != claims.tid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "tenant management is scoped to your active tenant; "
                "switch to this tenant (and re-authenticate) before editing it"
            ),
        )


def _validate_slug(slug: str | None) -> None:
    if slug is not None and slug != "" and not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="slug must be lowercase alphanumeric with single hyphens (e.g. 'kyiv-clinic')",
        )


# ── List / current / detail ──────────────────────────────────────────────


@router.get("", response_model=TenantListOut, summary="Tenants the caller belongs to")
async def list_tenants(claims: Annotated[Claims, Depends(current_user)]) -> TenantListOut:
    state = get_state()
    async with tenant_connection(state.tenant_writer_pool, claims.tid) as conn:
        rows = await repo.list_tenants_for_user(conn, user_sub=claims.sub)
    return TenantListOut(items=[_summary(r, r["membership_role"]) for r in rows])


@router.get(
    "/current",
    response_model=TenantOut,
    summary="The caller's active tenant (from the JWT tid)",
)
async def current_tenant(
    claims: Annotated[Claims, Depends(requires("tenant.read", "tenant"))],
) -> TenantOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.get_tenant(conn, tenant_id=claims.tid)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")
    my_role = await _my_membership_role(claims.tid, claims.sub)
    return _tenant_out(row, my_role=my_role)


@router.get("/{tenant_id}", response_model=TenantOut, summary="Tenant profile / branding")
async def get_tenant(
    tenant_id: UUID,
    claims: Annotated[Claims, Depends(requires("tenant.read", "tenant"))],
) -> TenantOut:
    my_role = await _require_member(tenant_id, claims)
    state = get_state()
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        row = await repo.get_tenant(conn, tenant_id=tenant_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")
    return _tenant_out(row, my_role=my_role)


# ── Create ───────────────────────────────────────────────────────────────


@router.post(
    "",
    response_model=TenantOut,
    status_code=status.HTTP_201_CREATED,
    summary="Onboard a new clinic/tenant; the caller becomes its owner",
    dependencies=[Depends(requires_mfa())],
)
async def create_tenant(
    body: TenantCreate,
    claims: Annotated[Claims, Depends(requires("tenant.create", "tenant"))],
) -> TenantOut:
    _validate_slug(body.slug)
    slug = body.slug or _slugify(body.name)
    state = get_state()
    try:
        async with tenant_connection(state.tenant_writer_pool, claims.tid) as conn:
            row = await repo.create_tenant(
                conn,
                name=body.name.strip(),
                display_name=body.display_name.strip(),
                slug=slug,
                legal_name=body.legal_name.strip(),
                locale=body.locale,
                timezone=body.timezone,
                contact_email=body.contact_email.strip(),
                phone_number=body.phone_number.strip(),
                website=body.website.strip(),
                address_line1=body.address_line1.strip(),
                address_line2=body.address_line2.strip(),
                postal_code=body.postal_code.strip(),
                city=body.city.strip(),
                state_or_region=body.state_or_region.strip(),
                country=body.country.strip(),
                tax_id=body.tax_id.strip(),
                registration_number=body.registration_number.strip(),
            )
            # The creator is the founding owner.
            await repo.add_member(
                conn,
                tenant_id=row["id"],
                user_sub=claims.sub,
                role="owner",
                invited_by=claims.sub,
                status="active",
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a tenant with this name or slug already exists",
        ) from exc

    await _audit(
        claims,
        tenant_id=row["id"],
        kind=audit_kinds.TENANT_CREATED,
        target_id=row["id"],
        payload={"name": row["name"], "slug": slug},
        severity=Severity.SEC,
    )
    return _tenant_out(row, my_role="owner")


# ── Update ───────────────────────────────────────────────────────────────


@router.patch(
    "/{tenant_id}",
    response_model=TenantOut,
    summary="Update tenant profile / branding / contact fields",
    dependencies=[Depends(requires_mfa())],
)
async def update_tenant(
    tenant_id: UUID,
    body: TenantUpdate,
    claims: Annotated[Claims, Depends(requires("tenant.update", "tenant"))],
) -> TenantOut:
    _require_active_tenant(tenant_id, claims)
    my_role = await _require_member(tenant_id, claims)
    _require_manager(my_role)
    _validate_slug(body.slug)

    fields: dict[str, Any] = body.model_dump(exclude_unset=True)
    # ``is_active`` is the simple toggle; keep the finer ``status`` in sync so
    # the two never contradict (active ⇄ suspended). An explicit status is not
    # part of the update surface — lifecycle beyond active/suspended is admin-CLI.
    if "is_active" in fields:
        fields["status"] = "active" if fields["is_active"] else "suspended"
    for key in ("display_name", "legal_name", "contact_email", "phone_number",
                "website", "address_line1", "address_line2", "postal_code",
                "city", "state_or_region", "country", "tax_id",
                "registration_number", "slug"):
        if key in fields and isinstance(fields[key], str):
            fields[key] = fields[key].strip()

    changed = sorted(body.model_dump(exclude_unset=True).keys())
    state = get_state()
    try:
        async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
            row = await repo.update_tenant(conn, tenant_id=tenant_id, fields=fields)
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="name or slug already in use"
        ) from exc
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    await _audit(
        claims,
        tenant_id=tenant_id,
        kind=audit_kinds.TENANT_UPDATED,
        target_id=tenant_id,
        payload={"fields": changed},
        severity=Severity.INFO,
    )
    return _tenant_out(row, my_role=my_role)


# ── Logo ─────────────────────────────────────────────────────────────────


@router.put(
    "/{tenant_id}/logo",
    response_model=TenantOut,
    summary="Upload or replace the tenant logo (multipart image, ≤2MB)",
    dependencies=[Depends(requires_mfa())],
)
async def upload_logo(
    tenant_id: UUID,
    claims: Annotated[Claims, Depends(requires("tenant.update", "tenant"))],
    file: Annotated[UploadFile | None, File()] = None,
    logo_url: Annotated[str | None, Form()] = None,
) -> TenantOut:
    _require_active_tenant(tenant_id, claims)
    my_role = await _require_member(tenant_id, claims)
    _require_manager(my_role)

    if file is None and not logo_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="provide an image `file` or a `logo_url`",
        )

    logo_bytes: bytes | None = None
    content_type = ""
    if file is not None:
        content_type = file.content_type or ""
        if not content_type.startswith("image/"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="logo must be an image/* file",
            )
        logo_bytes = await file.read()
        if len(logo_bytes) > _MAX_LOGO_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="logo exceeds the 2MB limit",
            )

    state = get_state()
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        row = await repo.set_logo(
            conn,
            tenant_id=tenant_id,
            logo_bytes=logo_bytes,
            logo_content_type=content_type,
            logo_url=(logo_url or "").strip(),
        )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    await _audit(
        claims,
        tenant_id=tenant_id,
        kind=audit_kinds.TENANT_LOGO_UPDATED,
        target_id=tenant_id,
        payload={"inline": logo_bytes is not None, "size_bytes": len(logo_bytes or b"")},
        severity=Severity.INFO,
    )
    return _tenant_out(row, my_role=my_role)


@router.get(
    "/{tenant_id}/logo",
    summary="Fetch the tenant logo bytes (for branding / PDF preview)",
    responses={200: {"content": {"image/*": {}}}},
)
async def get_logo(
    tenant_id: UUID,
    claims: Annotated[Claims, Depends(requires("tenant.read", "tenant"))],
) -> Response:
    await _require_member(tenant_id, claims)
    state = get_state()
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        logo = await repo.get_logo(conn, tenant_id=tenant_id)
    if logo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no logo set")
    data, content_type = logo
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "private, max-age=300"})


# ── Members ──────────────────────────────────────────────────────────────


@router.get(
    "/{tenant_id}/members",
    response_model=MemberListOut,
    summary="List the tenant's members",
)
async def list_members(
    tenant_id: UUID,
    claims: Annotated[Claims, Depends(requires("tenant.read", "tenant"))],
) -> MemberListOut:
    await _require_member(tenant_id, claims)
    state = get_state()
    # Read on the writer pool so cross-tenant members you belong to also resolve.
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        rows = await repo.list_members(conn, tenant_id=tenant_id)
    return MemberListOut(items=[_member_out(r) for r in rows])


@router.post(
    "/{tenant_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
    summary="Link a principal to the tenant with a management role",
    dependencies=[Depends(requires_mfa())],
)
async def add_member(
    tenant_id: UUID,
    body: MemberAdd,
    claims: Annotated[Claims, Depends(requires("tenant.manage_members", "tenant"))],
) -> MemberOut:
    _require_active_tenant(tenant_id, claims)
    _require_manager(await _require_member(tenant_id, claims))
    _validate_management_role(body.role)

    state = get_state()
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        user_sub = body.user_sub
        if user_sub is None:
            if not body.email:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="provide a `user_sub` or an `email`",
                )
            user_sub = await repo.resolve_sub_by_email(conn, email=body.email.strip())
            if user_sub is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="no user with that email in this tenant; add by user_sub instead",
                )
        try:
            member = await repo.add_member(
                conn,
                tenant_id=tenant_id,
                user_sub=user_sub,
                role=body.role,
                invited_by=claims.sub,
                status="active",
            )
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="this user is already a member of the tenant",
            ) from exc

    await _audit(
        claims,
        tenant_id=tenant_id,
        kind=audit_kinds.TENANT_MEMBER_ADDED,
        target_id=user_sub,
        payload={"role": body.role},
        severity=Severity.SEC,
    )
    # Re-read via list to get the joined profile fields for the response.
    return MemberOut(
        user_sub=member["user_sub"],
        role=member["role"],
        status=member["status"],
        created_at=member["created_at"],
        updated_at=member["updated_at"],
    )


@router.patch(
    "/{tenant_id}/members/{sub}",
    response_model=MemberOut,
    summary="Change a member's management role",
    dependencies=[Depends(requires_mfa())],
)
async def update_member(
    tenant_id: UUID,
    sub: UUID,
    body: MemberRoleUpdate,
    claims: Annotated[Claims, Depends(requires("tenant.manage_members", "tenant"))],
) -> MemberOut:
    _require_active_tenant(tenant_id, claims)
    _require_manager(await _require_member(tenant_id, claims))
    _validate_management_role(body.role)

    state = get_state()
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        existing = await repo.get_membership(conn, tenant_id=tenant_id, user_sub=sub)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
        # Never leave a tenant ownerless.
        if existing["role"] == "owner" and body.role != "owner":
            remaining = await repo.count_active_owners(conn, tenant_id=tenant_id, exclude_sub=sub)
            if remaining == 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="cannot demote the last owner of the tenant",
                )
        member = await repo.update_member_role(
            conn, tenant_id=tenant_id, user_sub=sub, role=body.role
        )

    await _audit(
        claims,
        tenant_id=tenant_id,
        kind=audit_kinds.TENANT_MEMBER_ROLE_CHANGED,
        target_id=sub,
        payload={"old_role": existing["role"], "new_role": body.role},
        severity=Severity.SEC,
    )
    assert member is not None
    return MemberOut(
        user_sub=member["user_sub"],
        role=member["role"],
        status=member["status"],
        created_at=member["created_at"],
        updated_at=member["updated_at"],
    )


@router.delete(
    "/{tenant_id}/members/{sub}",
    status_code=status.HTTP_200_OK,
    summary="Remove a member from the tenant",
    dependencies=[Depends(requires_mfa())],
)
async def remove_member(
    tenant_id: UUID,
    sub: UUID,
    claims: Annotated[Claims, Depends(requires("tenant.manage_members", "tenant"))],
) -> dict[str, Any]:
    _require_active_tenant(tenant_id, claims)
    _require_manager(await _require_member(tenant_id, claims))

    state = get_state()
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        existing = await repo.get_membership(conn, tenant_id=tenant_id, user_sub=sub)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
        if existing["role"] == "owner":
            remaining = await repo.count_active_owners(conn, tenant_id=tenant_id, exclude_sub=sub)
            if remaining == 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="cannot remove the last owner of the tenant",
                )
        await repo.remove_member(conn, tenant_id=tenant_id, user_sub=sub)

    await _audit(
        claims,
        tenant_id=tenant_id,
        kind=audit_kinds.TENANT_MEMBER_REMOVED,
        target_id=sub,
        payload={"role": existing["role"]},
        severity=Severity.SEC,
    )
    return {"tenant_id": str(tenant_id), "user_sub": str(sub), "removed": True}


# ── Switch active tenant ─────────────────────────────────────────────────


@router.post(
    "/{tenant_id}/switch",
    response_model=SwitchOut,
    summary="Select the caller's active tenant (must be a member)",
)
async def switch_tenant(
    tenant_id: UUID,
    claims: Annotated[Claims, Depends(current_user)],
) -> SwitchOut:
    """Validate that the caller may act as ``tenant_id`` and record the switch.

    NOTE: the access token's ``tid`` — which drives RLS — is issued by
    Keycloak and is single-tenant in the pilot. A full switch therefore
    requires the SPA to obtain a token re-scoped to ``tenant_id`` (future
    Keycloak per-tenant identity work). This endpoint is the authorization
    gate + audit hook for that flow, and returns the selected tenant so the
    SPA can update its active-tenant UI immediately.
    """
    my_role = await _require_member(tenant_id, claims)
    state = get_state()
    async with tenant_connection(state.tenant_writer_pool, tenant_id) as conn:
        row = await repo.get_tenant(conn, tenant_id=tenant_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")

    await _audit(
        claims,
        tenant_id=tenant_id,
        kind=audit_kinds.TENANT_SWITCHED,
        target_id=tenant_id,
        payload={"from": str(claims.tid), "to": str(tenant_id)},
        severity=Severity.INFO,
    )
    note = (
        "active tenant selected"
        if tenant_id == claims.tid
        else "re-authenticate to obtain a token scoped to this tenant before accessing its data"
    )
    return SwitchOut(active_tenant=_summary(row, my_role), note=note)


# ── helpers ──────────────────────────────────────────────────────────────


def _require_manager(role: str) -> None:
    if role not in MANAGER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"membership role {role!r} may not manage this tenant (owner/admin only)",
        )


def _validate_management_role(role: str) -> None:
    if role not in MANAGEMENT_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"role must be one of {sorted(MANAGEMENT_ROLES)}",
        )


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "tenant"


async def _audit(
    claims: Claims,
    *,
    tenant_id: UUID,
    kind: str,
    target_id: UUID,
    payload: dict[str, Any],
    severity: Severity,
) -> None:
    state = get_state()
    try:
        await state.audit_writer.write_event(
            tenant_id=tenant_id,
            kind=kind,
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="tenant",
            target_id=str(target_id),
            payload=payload,
            severity=severity,
        )
    except Exception as exc:  # pragma: no cover - audit must never break the write
        logger.warning("tenant.audit_write_failed", extra={"kind": kind, "error": str(exc)})
