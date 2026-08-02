"""/v1/synonyms — tenant synonym-group curation (sprint 15, ADR-0038).

Minimal CRUD for tenant_admins ahead of the sprint-17 admin UI. System
groups are visible but immutable here — RLS has no PERMISSIVE write
policy for them, so even a bug in this router cannot touch the seeded
dictionary. Terms are dictionary entries (closed vocabulary), not PHI.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import synonyms_repository as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/synonyms", tags=["synonyms"])


class SynonymGroupOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: UUID
    source: Literal["system", "tenant"]
    language: Literal["uk", "en"]
    terms: list[str]


class SynonymGroupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terms: list[str] = Field(min_length=2, max_length=12)
    language: Literal["uk", "en"]

    @field_validator("terms")
    @classmethod
    def _terms_shape(cls, terms: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for term in terms:
            t = term.strip()
            if not (1 <= len(t) <= 120):
                raise ValueError("each term must be 1..120 chars after trim")
            if t.lower() in seen:
                raise ValueError(f"duplicate term: {t}")
            seen.add(t.lower())
            cleaned.append(t)
        return cleaned


@router.get("", response_model=list[SynonymGroupOut])
async def list_synonym_groups(
    claims: Annotated[Claims, Depends(requires("synonym.read", "synonym"))],
) -> list[SynonymGroupOut]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        groups = await repo.list_groups(conn)
    return [
        SynonymGroupOut(
            group_id=g.group_id, source=g.source, language=g.language, terms=g.terms
        )
        for g in groups
    ]


@router.post("", response_model=SynonymGroupOut, status_code=status.HTTP_201_CREATED)
async def create_synonym_group(
    body: SynonymGroupBody,
    claims: Annotated[Claims, Depends(requires("synonym.write", "synonym"))],
) -> SynonymGroupOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        group_id = await repo.create_group(
            conn,
            tenant_id=claims.tid,
            terms=body.terms,
            language=body.language,
            created_by=claims.sub,
        )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.SYNONYM_GROUP_CREATED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="synonym",
        target_id=group_id,
        payload={"group_id": str(group_id), "term_count": len(body.terms),
                 "language": body.language},
        severity=Severity.INFO,
    )
    return SynonymGroupOut(
        group_id=group_id, source="tenant", language=body.language, terms=sorted(body.terms)
    )


@router.put("/{group_id}", response_model=SynonymGroupOut)
async def update_synonym_group(
    group_id: UUID,
    body: SynonymGroupBody,
    claims: Annotated[Claims, Depends(requires("synonym.write", "synonym"))],
) -> SynonymGroupOut:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        replaced = await repo.replace_group(
            conn,
            tenant_id=claims.tid,
            group_id=group_id,
            terms=body.terms,
            language=body.language,
            created_by=claims.sub,
        )
    if not replaced:
        # Not found OR a system group (immutable — RLS shows it but never
        # lets tenant writes touch it).
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="tenant synonym group not found"
        )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.SYNONYM_GROUP_UPDATED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="synonym",
        target_id=group_id,
        payload={"group_id": str(group_id), "term_count": len(body.terms),
                 "language": body.language},
        severity=Severity.INFO,
    )
    return SynonymGroupOut(
        group_id=group_id, source="tenant", language=body.language, terms=sorted(body.terms)
    )


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_synonym_group(
    group_id: UUID,
    claims: Annotated[Claims, Depends(requires("synonym.write", "synonym"))],
) -> None:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        deleted = await repo.delete_group(conn, group_id=group_id)
    if not deleted:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="tenant synonym group not found"
        )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.SYNONYM_GROUP_DELETED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="synonym",
        target_id=group_id,
        payload={"group_id": str(group_id)},
        severity=Severity.INFO,
    )
