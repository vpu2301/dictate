"""Allowlist admin: POST /web/domains, PATCH /web/domains/{id}, GET /web/domains.

`evidence.domains.manage` on target kind `evidence_corpus` (knowledge_admin,
tenant_admin) — the permission already exists in the shared matrix from S01.
Every change is a security-severity audit event: who added what, and when.

Shipped GLOBAL rows are read-only for tenants (RLS denies the UPDATE). A
tenant that wants a default off inserts its own row with `status='disabled'`,
which shadows the global one — the shipped table stays a reviewed artifact
rather than something 40 tenants have quietly diverged from.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from audit import Severity
from auth import Claims
from auth.perms import Action, TargetKind
from db import tenant_connection
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from evidence_websearch import audit_kinds
from evidence_websearch.adapters import pg
from evidence_websearch.deps import requires
from evidence_websearch.main_deps import get_state

logger = logging.getLogger(__name__)
router = APIRouter(tags=["domains"])

_MANAGE: Action = "evidence.domains.manage"
_TARGET: TargetKind = "evidence_corpus"

_TRUST_TIERS = {
    "international_organization",
    "government",
    "professional_society",
    "guideline_registry",
    "journal",
    "other",
}
_STATUSES = {"enabled", "pending", "disabled"}
# Registrable hostname; mirrors the CHECK constraint in migration 0070 so a
# bad request is a 422 with a code, not a 500 from the database.
_HOSTNAME = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"


class DomainCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str = Field(pattern=_HOSTNAME, max_length=253)
    trust_tier: str
    # New entries land as `pending` by default: allowlisting is a review step,
    # not a self-service toggle (FR-6).
    status: str = "pending"
    metadata_only: bool = False
    notes: str | None = Field(default=None, max_length=500)


class DomainPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trust_tier: str | None = None
    status: str | None = None
    metadata_only: bool | None = None
    notes: str | None = Field(default=None, max_length=500)


class DomainView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    domain: str
    trust_tier: str
    status: str
    is_default: bool
    metadata_only: bool
    notes: str | None = None
    # True for the shipped GLOBAL rows: visible to everyone, editable by nobody.
    shipped: bool = False


def _view(row: pg.DomainRow) -> DomainView:
    from evidence_websearch.domain.allowlist import GLOBAL_TENANT

    return DomainView(
        id=row.id,
        domain=row.domain,
        trust_tier=row.trust_tier,
        status=row.status,
        is_default=row.is_default,
        metadata_only=row.metadata_only,
        notes=row.notes,
        shipped=row.tenant_id == GLOBAL_TENANT,
    )


def _validate(trust_tier: str | None, status: str | None) -> None:
    if trust_tier is not None and trust_tier not in _TRUST_TIERS:
        raise HTTPException(status_code=422, detail=f"domain_invalid: trust_tier {trust_tier!r}")
    if status is not None and status not in _STATUSES:
        raise HTTPException(status_code=422, detail=f"domain_invalid: status {status!r}")


@router.get("/web/domains")
async def list_domains(
    claims: Annotated[Claims, Depends(requires(_MANAGE, _TARGET))],
) -> list[DomainView]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await pg.list_domains(conn)
    return [_view(row) for row in rows]


@router.post("/web/domains", status_code=201)
async def create_domain(
    body: DomainCreate,
    claims: Annotated[Claims, Depends(requires(_MANAGE, _TARGET))],
) -> DomainView:
    _validate(body.trust_tier, body.status)
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await pg.insert_domain(
            conn,
            tenant_id=claims.tid,
            domain=body.domain.casefold(),
            trust_tier=body.trust_tier,
            status=body.status,
            metadata_only=body.metadata_only,
            notes=body.notes,
            added_by=claims.sub,
        )
    if row is None:
        raise HTTPException(status_code=409, detail="domain_exists")
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.WEB_DOMAIN_ADDED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind=_TARGET,
        target_id=str(row.id),
        payload={
            "domain": row.domain,
            "trust_tier": row.trust_tier,
            "status": row.status,
            "metadata_only": str(row.metadata_only),
        },
        severity=Severity.SEC,
    )
    return _view(row)


@router.patch("/web/domains/{domain_id}")
async def patch_domain(
    domain_id: UUID,
    body: DomainPatch,
    claims: Annotated[Claims, Depends(requires(_MANAGE, _TARGET))],
) -> DomainView:
    _validate(body.trust_tier, body.status)
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await pg.update_domain(
            conn,
            domain_id=domain_id,
            trust_tier=body.trust_tier,
            status=body.status,
            metadata_only=body.metadata_only,
            notes=body.notes,
            reviewed_by=claims.sub,
        )
    # A shipped GLOBAL row is visible but not updatable: RLS filters it out of
    # the UPDATE, so it reads as not-found — the platform's cross-tenant rule
    # (PD3: never "forbidden", always "not found").
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    kind = (
        audit_kinds.WEB_DOMAIN_DISABLED
        if row.status == "disabled"
        else audit_kinds.WEB_DOMAIN_ADDED
    )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=kind,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind=_TARGET,
        target_id=str(row.id),
        payload={
            "domain": row.domain,
            "trust_tier": row.trust_tier,
            "status": row.status,
            "metadata_only": str(row.metadata_only),
        },
        severity=Severity.SEC,
    )
    return _view(row)
