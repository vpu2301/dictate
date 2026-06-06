"""``/templates`` endpoints (sprint 06)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from opentelemetry import metrics

from audit import Severity
from auth import Claims
from db import tenant_connection
from template_models import EditKind, TemplateDefinition

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import repository
from ..domain.cache import CachedTemplate
from ..domain.repository import section_prompt_from_jsonb

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/templates", tags=["templates"])

_meter = metrics.get_meter("mdx.templates")
_clones = _meter.create_counter("mdx_template_clones_total", unit="1")
_updates = _meter.create_counter("mdx_template_updates_total", unit="1")
_deprecations = _meter.create_counter("mdx_template_deprecations_total", unit="1")
_section_lookups = _meter.create_counter(
    "mdx_template_section_prompt_lookups_total", unit="1"
)


# ── Wire models ─────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TemplateSummary(_Strict):
    id: UUID
    tenant_id: UUID | None
    parent_template_id: UUID | None
    code: str
    name: str
    language: str
    specialty: str
    schema_version: int
    is_system: bool
    status: str
    created_at: datetime
    updated_at: datetime


class TemplateDetail(_Strict):
    id: UUID
    tenant_id: UUID | None
    parent_template_id: UUID | None
    code: str
    name: str
    language: str
    specialty: str
    schema_version: int
    is_system: bool
    status: str
    schema_jsonb: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class CloneRequest(_Strict):
    system_template_id: UUID
    new_name: str | None = Field(default=None, min_length=1, max_length=256)
    new_code: str | None = Field(default=None, min_length=1, max_length=64)


class CloneResponse(_Strict):
    id: UUID


class UpdateResponse(_Strict):
    id: UUID
    kind: str  # 'cosmetic' | 'structural' | 'no_change'


class SectionPromptResponse(_Strict):
    prompt: str
    language: str
    section_name: str


# ── List ────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=list[TemplateSummary],
    summary="List visible templates (own-tenant + system).",
)
async def list_templates(
    claims: Annotated[Claims, Depends(requires("template.read", "template"))],
    specialty: Annotated[str | None, Query(max_length=64)] = None,
    language: Annotated[str | None, Query(pattern="^(uk|en)$")] = None,
    tenant_only: Annotated[bool, Query()] = False,
    include_deprecated: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[TemplateSummary]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await repository.list_templates(
            conn,
            tenant_only=tenant_only,
            specialty=specialty,
            language=language,
            include_deprecated=include_deprecated,
            limit=limit,
        )
    return [
        TemplateSummary(
            id=r["id"],
            tenant_id=r["tenant_id"],
            parent_template_id=r["parent_template_id"],
            code=r["code"],
            name=r["name"],
            language=r["language"],
            specialty=r["specialty"],
            schema_version=int(r["schema_version"]),
            is_system=bool(r["is_system"]),
            status=r["status"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


# ── Get ─────────────────────────────────────────────────────────────


@router.get(
    "/{template_id}",
    response_model=TemplateDetail,
    summary="Fetch full template definition (cached).",
)
async def get_template(
    template_id: UUID,
    claims: Annotated[Claims, Depends(requires("template.read", "template"))],
) -> TemplateDetail:
    state = get_state()
    cached = state.template_cache.get(tenant_id=claims.tid, template_id=template_id)
    if cached is not None:
        # Read row metadata from cached object; for created_at / updated_at
        # we still need a small read. To keep it fast for the hot path we
        # cache the metadata, not just schema_jsonb, in a future revision.
        # Sprint 06: cache holds schema_jsonb only; do a cheap row read here.
        pass

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repository.get_template(conn, template_id=template_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    raw = row["schema_jsonb"]
    if isinstance(raw, str):
        raw = json.loads(raw)

    state.template_cache.put(
        tenant_id=claims.tid,
        template_id=template_id,
        cached=CachedTemplate(
            template_id=template_id,
            tenant_id=row["tenant_id"],
            schema_jsonb=raw,
            schema_version=int(row["schema_version"]),
            status=row["status"],
        ),
    )

    # Audit full-template view (heavier than the list endpoint).
    try:
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind=audit_kinds.TEMPLATE_VIEWED_FULL,
            actor_sub=claims.sub,
            target_kind="template",
            target_id=str(template_id),
            payload={"is_system": bool(row["is_system"])},
            severity=Severity.INFO,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "template.viewed_full.audit_write_failed",
            extra={"error": str(exc), "error_class": type(exc).__name__},
        )

    return TemplateDetail(
        id=row["id"],
        tenant_id=row["tenant_id"],
        parent_template_id=row["parent_template_id"],
        code=row["code"],
        name=row["name"],
        language=row["language"],
        specialty=row["specialty"],
        schema_version=int(row["schema_version"]),
        is_system=bool(row["is_system"]),
        status=row["status"],
        schema_jsonb=raw,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Section prompt lookup (dictation hot path) ──────────────────────


@router.get(
    "/{template_id}/sections/{section_id}/prompt",
    response_model=SectionPromptResponse,
    summary="Fetch a section's ASR prompt (cached; dictation hot path).",
)
async def get_section_prompt(
    template_id: UUID,
    section_id: str,
    claims: Annotated[Claims, Depends(requires("template.read", "template"))],
) -> SectionPromptResponse:
    state = get_state()
    cached = state.template_cache.get(
        tenant_id=claims.tid, template_id=template_id
    )
    if cached is None:
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            row = await repository.get_template(conn, template_id=template_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        raw = row["schema_jsonb"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        cached = CachedTemplate(
            template_id=template_id,
            tenant_id=row["tenant_id"],
            schema_jsonb=raw,
            schema_version=int(row["schema_version"]),
            status=row["status"],
        )
        state.template_cache.put(
            tenant_id=claims.tid, template_id=template_id, cached=cached
        )

    result = section_prompt_from_jsonb(cached.schema_jsonb, section_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"section {section_id!r} not in template",
        )
    prompt, language, section_name = result
    _section_lookups.add(1)
    return SectionPromptResponse(
        prompt=prompt, language=language, section_name=section_name
    )


# ── Clone ───────────────────────────────────────────────────────────


@router.post(
    "/clone",
    response_model=CloneResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Clone a visible template into the caller's tenant.",
)
async def clone_template(
    body: CloneRequest,
    claims: Annotated[Claims, Depends(requires("template.clone", "template"))],
) -> CloneResponse:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        new_id = await repository.clone_template(
            conn,
            tenant_id=claims.tid,
            source_id=body.system_template_id,
            new_name=body.new_name,
            new_code=body.new_code,
        )
    if new_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="source template not visible",
        )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.TEMPLATE_CLONED,
        actor_sub=claims.sub,
        target_kind="template",
        target_id=str(new_id),
        payload={"source_id": str(body.system_template_id)},
        severity=Severity.INFO,
    )
    _clones.add(1, {"source_template_id": str(body.system_template_id)})
    return CloneResponse(id=new_id)


# ── Update (cosmetic vs structural) ─────────────────────────────────


@router.put(
    "/{template_id}",
    response_model=UpdateResponse,
    summary="Update a tenant template. Structural edits create a new version.",
)
async def update_template(
    template_id: UUID,
    body: TemplateDefinition,
    claims: Annotated[Claims, Depends(requires("template.update", "template"))],
) -> UpdateResponse:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        try:
            result_id, kind = await repository.update_template(
                conn, template_id=template_id, new_definition=body
            )
        except ValueError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

    # Invalidate cache entries: old id always; new id only if a new row.
    state.template_cache.invalidate(tenant_id=claims.tid, template_id=template_id)
    if result_id != template_id:
        state.template_cache.invalidate(tenant_id=claims.tid, template_id=result_id)

    if kind == EditKind.NO_CHANGE:
        return UpdateResponse(id=template_id, kind="no_change")

    audit_kind = (
        audit_kinds.TEMPLATE_VERSIONED
        if kind == EditKind.STRUCTURAL
        else audit_kinds.TEMPLATE_UPDATED
    )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kind,
        actor_sub=claims.sub,
        target_kind="template",
        target_id=str(result_id),
        payload={"source_id": str(template_id), "kind": str(kind)},
        severity=Severity.INFO,
    )
    _updates.add(1, {"kind": kind.value})
    return UpdateResponse(id=result_id, kind=kind.value)


# ── Deprecate (soft-delete) ─────────────────────────────────────────


@router.delete(
    "/{template_id}",
    summary="Soft-delete (deprecate) a tenant template.",
)
async def deprecate_template(
    template_id: UUID,
    claims: Annotated[Claims, Depends(requires("template.deprecate", "template"))],
) -> dict[str, str]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        outcome = await repository.deprecate_template(
            conn, template_id=template_id
        )
    if outcome == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if outcome == "in_use":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "templates referenced by reports cannot be deprecated; "
                "sprint-17 admin will offer re-bind"
            ),
        )

    state.template_cache.invalidate(tenant_id=claims.tid, template_id=template_id)
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.TEMPLATE_DEPRECATED,
        actor_sub=claims.sub,
        target_kind="template",
        target_id=str(template_id),
        payload={},
        severity=Severity.INFO,
    )
    _deprecations.add(1)
    return {"status": "deprecated"}
