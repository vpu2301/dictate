"""POST /autocomplete/suggest."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from auth import Claims
from db import tenant_connection

from .. import repository as repo
from .. import suggest as sug
from ..config import settings
from ..deps import get_state, requires, role_for_rls

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/autocomplete", tags=["autocomplete"])


class SuggestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str = Field(min_length=1, max_length=80)
    language: Literal["uk", "en"]
    limit: int = Field(default=settings.suggest_default_limit, ge=1, le=settings.suggest_max_limit)
    context: dict | None = Field(
        default=None,
        description=(
            "Optional editor context (e.g. {field}). Accepted and forwarded "
            "to telemetry only — sprint 10 does NO context-aware ranking; "
            "do not expect behavior from it."
        ),
    )


class SuggestionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["phrase", "snippet"]
    text: str
    completion: str
    source: str
    confidence: float
    cursor_offset: int | None = None


class SuggestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    suggestions: list[SuggestionDTO]


@router.post("/suggest", response_model=SuggestResponse)
async def suggest(
    body: SuggestRequest,
    claims: Annotated[Claims, Depends(requires("autocomplete.read", "phrase"))],
) -> SuggestResponse:
    state = get_state()
    request_id = uuid.uuid4()
    t0 = time.perf_counter()

    def _observe(path: str) -> None:
        # Interface for the k6 gate + Grafana p95 panel (steps 07/08):
        # path ∈ hit | miss | degraded | snippet.
        state.suggest_latency_metric.record(
            (time.perf_counter() - t0) * 1000.0, {"path": path}
        )

    # Snippet path: leading slash → trigger lookup.
    if sug.is_snippet_prefix(body.prefix):
        trigger = sug.extract_snippet_trigger(body.prefix)
        if not trigger:
            _observe("snippet")
            return SuggestResponse(request_id=request_id, suggestions=[])
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            await _set_user_ctx(conn, claims)
            row = await repo.fetch_snippet(conn, trigger=trigger, language=body.language)
        if row is None:
            _observe("snippet")
            return SuggestResponse(request_id=request_id, suggestions=[])
        s = sug.snippet_suggestion(
            snippet_id=str(row["id"]),
            expansion=row["expansion"],
            cursor_position=int(row["cursor_position"]),
            source=row["source"],
            trigger=trigger,
        )
        _observe("snippet")
        return SuggestResponse(
            request_id=request_id,
            suggestions=[
                SuggestionDTO(
                    id=s.id,
                    kind=s.kind,
                    text=s.text,
                    completion=s.completion,
                    source=s.source,
                    confidence=s.confidence,
                    cursor_offset=s.cursor_offset,
                )
            ],
        )

    # Trie path.
    async def _build() -> sug.TenantTrie:  # noqa: F821
        from autocomplete_service.trie.builder import build_trie_from_phrases

        async with tenant_connection(state.app_pool, claims.tid) as conn:
            await _set_user_ctx(conn, claims)
            rows = await repo.fetch_corpus(conn, language=body.language)
        return build_trie_from_phrases(
            tenant_id=str(claims.tid),
            language=body.language,
            user_id=str(claims.sub),
            rows=rows,
        )

    trie, status = await state.trie_cache.get_or_build(
        tenant_id=claims.tid,
        language=body.language,
        user_id=claims.sub,
        build_fn=_build,
    )
    state.suggest_cache_metric.add(1, {"hit": str(status == "hit").lower()})

    suggestions = sug.suggest_from_trie(trie=trie, prefix=body.prefix, limit=body.limit)
    _observe(status)
    return SuggestResponse(
        request_id=request_id,
        suggestions=[
            SuggestionDTO(
                id=s.id,
                kind=s.kind,
                text=s.text,
                completion=s.completion,
                source=s.source,
                confidence=s.confidence,
                cursor_offset=s.cursor_offset,
            )
            for s in suggestions
        ],
    )


async def _set_user_ctx(conn, claims: Claims) -> None:
    """Set the GUC keys ``write_user_phrases`` policy depends on."""
    await conn.execute("SELECT set_config('app.user_id',   $1, true)", str(claims.sub))
    await conn.execute(
        "SELECT set_config('app.user_role', $1, true)",
        role_for_rls(claims),
    )
