"""Phrase + snippet CRUD endpoints."""

from __future__ import annotations

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from .. import repository as repo
from ..deps import get_state, requires
from ..scrubber import contains_pii

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/autocomplete", tags=["autocomplete"])


# ── Phrases ─────────────────────────────────────────────────────────


class CreatePhraseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phrase: str = Field(min_length=1, max_length=80)
    language: Literal["uk", "en"]
    specialty: str | None = None
    section_hint: str | None = None
    source: Literal["user", "tenant"] = "user"


class PhraseDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    phrase: str
    language: str
    specialty: str | None
    section_hint: str | None
    source: str
    impression_count: int
    acceptance_count: int


@router.post("/phrases", response_model=PhraseDTO, status_code=status.HTTP_201_CREATED)
async def create_phrase(
    body: CreatePhraseRequest,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> PhraseDTO:
    state = get_state()
    pii_hits = contains_pii(body.phrase)
    if pii_hits:
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind=audit_kinds.PHRASE_WRITE_REJECTED_PII,
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="autocomplete_phrases",
            target_id=None,
            payload={"patterns": pii_hits},
            severity=Severity.SEC,
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "phrase_contains_pii",
                "patterns": pii_hits,
                "message": "phrases must not contain patient-identifying information",
            },
        )

    owner_user_id = claims.sub if body.source == "user" else None
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await conn.execute("SELECT set_config('app.user_id',   $1, true)", str(claims.sub))
        await conn.execute(
            "SELECT set_config('app.user_role', $1, true)",
            (claims.roles[0] if claims.roles else "clinician"),
        )
        phrase_id = await repo.insert_phrase(
            conn,
            phrase=body.phrase,
            language=body.language,
            specialty=body.specialty,
            section_hint=body.section_hint,
            source=body.source,
            tenant_id=claims.tid,
            owner_user_id=owner_user_id,
        )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.PHRASE_CREATED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="autocomplete_phrases",
        target_id=phrase_id,
        payload={"source": body.source, "language": body.language},
        severity=Severity.INFO,
    )
    await state.trie_cache.bump_version_tag(tenant_id=claims.tid)
    return PhraseDTO(
        id=phrase_id,
        phrase=body.phrase,
        language=body.language,
        specialty=body.specialty,
        section_hint=body.section_hint,
        source=body.source,
        impression_count=0,
        acceptance_count=0,
    )


@router.delete("/phrases/{phrase_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_phrase(
    phrase_id: UUID,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> None:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await conn.execute("SELECT set_config('app.user_id',   $1, true)", str(claims.sub))
        await conn.execute(
            "SELECT set_config('app.user_role', $1, true)",
            (claims.roles[0] if claims.roles else "clinician"),
        )
        deleted = await repo.soft_delete_phrase(conn, phrase_id=phrase_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="phrase not found")
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.PHRASE_DELETED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="autocomplete_phrases",
        target_id=phrase_id,
        payload={},
        severity=Severity.INFO,
    )
    await state.trie_cache.bump_version_tag(tenant_id=claims.tid)
