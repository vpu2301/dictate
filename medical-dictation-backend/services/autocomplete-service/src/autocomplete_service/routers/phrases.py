"""Phrase + snippet CRUD endpoints."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from .. import repository as repo
from ..deps import get_state, requires, role_for_rls
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


class PhraseListItemDTO(BaseModel):
    """Row of the sprint-17 admin listing (GET). Distinct from PhraseDTO:
    the listing carries the roll-up counters + timestamps the console
    shows; the POST echo never has real counter values."""

    model_config = ConfigDict(extra="forbid")
    id: UUID
    phrase: str
    language: str
    specialty: str | None
    section_hint: str | None
    source: str
    impression_count: int
    acceptance_count: int
    last_accepted_at: datetime | None
    created_at: datetime
    # Sprint 21 corpus provenance (ADR-0043) — admin filters/columns.
    review_state: str
    tier: int | None
    source_kind: str
    corpus_release: str | None


@router.get("/phrases", response_model=list[PhraseListItemDTO])
async def list_phrases(
    claims: Annotated[Claims, Depends(requires("autocomplete.read", "phrase"))],
    language: Annotated[Literal["uk", "en"] | None, Query()] = None,
    specialty: Annotated[str | None, Query(max_length=64)] = None,
    source: Annotated[Literal["system", "tenant", "user"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PhraseListItemDTO]:
    """Admin corpus listing. Visibility is the RLS policy's (system +
    own-tenant + own-user rows); a read needs no audit event."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await conn.execute("SELECT set_config('app.user_id',   $1, true)", str(claims.sub))
        await conn.execute(
            "SELECT set_config('app.user_role', $1, true)", role_for_rls(claims)
        )
        rows = await repo.list_phrases(
            conn,
            language=language,
            specialty=specialty,
            source=source,
            limit=limit,
        )
    return [
        PhraseListItemDTO(
            id=r["id"],
            phrase=r["phrase"],
            language=r["language"],
            specialty=r["specialty"],
            section_hint=r["section_hint"],
            source=str(r["source"]),
            impression_count=int(r["impression_count"]),
            acceptance_count=int(r["acceptance_count"]),
            last_accepted_at=r["last_accepted_at"],
            created_at=r["created_at"],
            review_state=r["review_state"],
            tier=r["tier"],
            source_kind=r["source_kind"],
            corpus_release=r["corpus_release"],
        )
        for r in rows
    ]


class RetirePhraseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    review_state: Literal["retired"]
    corpus_release: str | None  # non-null = the phrase shipped in a release


@router.post("/phrases/{phrase_id}/retire", response_model=RetirePhraseResponse)
async def retire_phrase(
    phrase_id: UUID,
    claims: Annotated[Claims, Depends(requires("autocomplete.write", "phrase"))],
) -> RetirePhraseResponse:
    """Sprint 21: retire, never delete — provenance survives, serving stops
    (the trie feeds only review_state='accepted'). Tenant/user rows only;
    system (released) rows are retired by the corpus operator (see
    docs/runbooks/corpus.md), so the admin console can't silently edit the
    global corpus."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await conn.execute("SELECT set_config('app.user_id',   $1, true)", str(claims.sub))
        await conn.execute(
            "SELECT set_config('app.user_role', $1, true)", role_for_rls(claims)
        )
        row = await repo.retire_phrase(conn, phrase_id=phrase_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="phrase not found in tenant scope, already retired, "
            "or system-scope (operator-managed)",
        )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.PHRASE_UPDATED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="phrase",
        target_id=phrase_id,
        payload={"action": "retire", "corpus_release": row["corpus_release"]},
    )
    return RetirePhraseResponse(
        id=row["id"], review_state="retired", corpus_release=row["corpus_release"]
    )


async def _reject_pii(
    state, claims: Claims, *, field: str, text: str, target_kind: str
) -> None:
    """PII in a corpus write: 422 naming the pattern class ONLY (never the
    match), a security audit event (text length, not text), a metric the
    step-07 spike alert reads."""
    pii_hits = contains_pii(text)
    if not pii_hits:
        return
    for pattern in pii_hits:
        state.pii_rejections_metric.add(1, {"pattern": pattern})
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.PHRASE_WRITE_REJECTED_PII,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind=target_kind,
        target_id=None,
        payload={"patterns": pii_hits, "field": field, "text_length": len(text)},
        severity=Severity.SEC,
    )
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "error": "pii_detected",
            "patterns": pii_hits,
            "field": field,
            "message": "містить дані, схожі на персональні — не збережено",
        },
    )


async def _check_rate_limit(state, claims: Claims) -> None:
    allowed, retry_after = await state.phrase_rate_limiter.check(user_id=claims.sub)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited", "retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )


@router.post("/phrases", response_model=PhraseDTO, status_code=status.HTTP_201_CREATED)
async def create_phrase(
    body: CreatePhraseRequest,
    claims: Annotated[Claims, Depends(requires("autocomplete.write", "phrase"))],
) -> PhraseDTO:
    state = get_state()
    await _check_rate_limit(state, claims)
    await _reject_pii(
        state, claims, field="phrase", text=body.phrase,
        target_kind="autocomplete_phrases",
    )

    owner_user_id = claims.sub if body.source == "user" else None
    try:
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            await conn.execute(
                "SELECT set_config('app.user_id',   $1, true)", str(claims.sub)
            )
            await conn.execute(
                "SELECT set_config('app.user_role', $1, true)",
                role_for_rls(claims),
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
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"error": "phrase_already_exists"},
        ) from None
    except asyncpg.InsufficientPrivilegeError:
        # RLS WITH CHECK rejection (e.g. clinician posting source='tenant').
        # Authority lives in the DB; map to a stable code, never SQLSTATE.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden_scope"},
        ) from None
    except asyncpg.CheckViolationError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "validation"},
        ) from None

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
    claims: Annotated[Claims, Depends(requires("autocomplete.write", "phrase"))],
) -> None:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await conn.execute("SELECT set_config('app.user_id',   $1, true)", str(claims.sub))
        await conn.execute(
            "SELECT set_config('app.user_role', $1, true)",
            role_for_rls(claims),
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


# ── Snippets (write surface; suggest-side lookup lives in suggest.py) ──


class CreateSnippetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Mirrors the DB trigger_format CHECK: latin slug, typed as /trigger.
    trigger: str = Field(
        min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9_-]{0,30}$",
        description="Latin slug (DB CHECK ^[a-z][a-z0-9_-]{0,30}$); typed with a leading / at request time.",
    )
    expansion: str = Field(min_length=1, max_length=4000)
    cursor_position: int = Field(default=0, ge=0)
    language: Literal["uk", "en"]
    source: Literal["user", "tenant"] = "user"

    @model_validator(mode="after")
    def _cursor_within_expansion(self) -> CreateSnippetRequest:
        # (A leading "/" is already excluded by the trigger pattern —
        # triggers are stored WITHOUT the slash typed at request time.)
        if self.cursor_position > len(self.expansion):
            msg = "cursor_position must be within the expansion"
            raise ValueError(msg)
        return self


class SnippetDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    trigger: str
    expansion: str
    cursor_position: int
    language: str
    source: str


class SnippetListItemDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    trigger: str
    expansion: str
    cursor_position: int
    language: str
    source: str
    created_at: datetime


@router.get("/snippets", response_model=list[SnippetListItemDTO])
async def list_snippets(
    claims: Annotated[Claims, Depends(requires("autocomplete.read", "phrase"))],
    language: Annotated[Literal["uk", "en"] | None, Query()] = None,
    source: Annotated[Literal["system", "tenant", "user"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[SnippetListItemDTO]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await conn.execute("SELECT set_config('app.user_id',   $1, true)", str(claims.sub))
        await conn.execute(
            "SELECT set_config('app.user_role', $1, true)", role_for_rls(claims)
        )
        rows = await repo.list_snippets(
            conn, language=language, source=source, limit=limit
        )
    return [
        SnippetListItemDTO(
            id=r["id"],
            trigger=r["trigger"],
            expansion=r["expansion"],
            cursor_position=int(r["cursor_position"]),
            language=r["language"],
            source=str(r["source"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.post("/snippets", response_model=SnippetDTO, status_code=status.HTTP_201_CREATED)
async def create_snippet(
    body: CreateSnippetRequest,
    claims: Annotated[Claims, Depends(requires("autocomplete.write", "phrase"))],
) -> SnippetDTO:
    state = get_state()
    await _check_rate_limit(state, claims)
    await _reject_pii(
        state, claims, field="trigger", text=body.trigger,
        target_kind="autocomplete_snippets",
    )
    await _reject_pii(
        state, claims, field="expansion", text=body.expansion,
        target_kind="autocomplete_snippets",
    )

    owner_user_id = claims.sub if body.source == "user" else None
    try:
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            await conn.execute(
                "SELECT set_config('app.user_id',   $1, true)", str(claims.sub)
            )
            await conn.execute(
                "SELECT set_config('app.user_role', $1, true)",
                role_for_rls(claims),
            )
            snippet_id = await repo.insert_snippet(
                conn,
                trigger=body.trigger,
                expansion=body.expansion,
                cursor_position=body.cursor_position,
                language=body.language,
                source=body.source,
                tenant_id=claims.tid,
                owner_user_id=owner_user_id,
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"error": "snippet_already_exists"},
        ) from None
    except asyncpg.InsufficientPrivilegeError:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden_scope"},
        ) from None
    except asyncpg.CheckViolationError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "validation"},
        ) from None

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.SNIPPET_CREATED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="autocomplete_snippets",
        target_id=snippet_id,
        payload={"source": body.source, "trigger": body.trigger},
        severity=Severity.INFO,
    )
    return SnippetDTO(
        id=snippet_id,
        trigger=body.trigger,
        expansion=body.expansion,
        cursor_position=body.cursor_position,
        language=body.language,
        source=body.source,
    )


@router.delete("/snippets/{snippet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_snippet(
    snippet_id: UUID,
    claims: Annotated[Claims, Depends(requires("autocomplete.write", "phrase"))],
) -> None:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        await conn.execute("SELECT set_config('app.user_id',   $1, true)", str(claims.sub))
        await conn.execute(
            "SELECT set_config('app.user_role', $1, true)",
            role_for_rls(claims),
        )
        deleted = await repo.soft_delete_snippet(conn, snippet_id=snippet_id)
    if not deleted:
        # RLS makes foreign rows look nonexistent — 404, never an oracle.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="snippet not found")
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.SNIPPET_DELETED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="autocomplete_snippets",
        target_id=snippet_id,
        payload={},
        severity=Severity.INFO,
    )
