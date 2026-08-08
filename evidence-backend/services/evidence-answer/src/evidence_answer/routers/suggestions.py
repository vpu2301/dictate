"""GET /suggestions — curated starter questions plus the caller's popular ones."""

from __future__ import annotations

from typing import Annotated

from auth import Claims
from auth.perms import Action, TargetKind
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from evidence_answer.adapters import pg
from evidence_answer.config import settings
from evidence_answer.deps import requires, user_connection
from evidence_answer.domain import suggestions as curated
from evidence_answer.main_deps import get_state

router = APIRouter(tags=["suggestions"])

_ASK: Action = "evidence.ask"
_TARGET: TargetKind = "evidence"


class SuggestionsView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    curated: list[str] = []
    # The caller's own most-asked questions (`questions` is user-private).
    popular: list[str] = []
    specialties: list[str] = []


@router.get("/suggestions")
async def get_suggestions(
    claims: Annotated[Claims, Depends(requires(_ASK, _TARGET))],
    specialty: Annotated[str | None, Query(max_length=64)] = None,
    locale: Annotated[str, Query(pattern=r"^(uk|en)(-[A-Za-z]{2})?$")] = "uk",
) -> SuggestionsView:
    state = get_state()
    async with user_connection(state.app_pool, tenant_id=claims.tid, user_sub=claims.sub) as conn:
        popular = await pg.popular_questions(
            conn, tenant_id=claims.tid, limit=settings.suggestions_limit
        )
    return SuggestionsView(
        curated=curated.curated_for(specialty, locale)[: settings.suggestions_limit],
        popular=popular,
        specialties=curated.specialties(),
    )
