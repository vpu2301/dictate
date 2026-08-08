"""GET /questions — the asker's own history, cursor-paged.

`questions` is user-private by RLS, so "history" means the caller's history
even though the query says tenant. That is deliberate: a clinician's question
list is closer to a search history than to a clinical record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from auth import Claims
from auth.perms import Action, TargetKind
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from evidence_answer.adapters import pg
from evidence_answer.config import settings
from evidence_answer.deps import requires, user_connection
from evidence_answer.main_deps import get_state

router = APIRouter(tags=["questions"])

_ASK: Action = "evidence.ask"
_TARGET: TargetKind = "evidence"


class QuestionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    text: str
    mode: str
    locale: str
    created_at: datetime
    answer_id: UUID | None = None
    answer_status: str | None = None


class QuestionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[QuestionView] = []
    # created_at of the last item; pass back as `cursor`. Null = end of list.
    next_cursor: datetime | None = None


@router.get("/questions")
async def list_questions(
    claims: Annotated[Claims, Depends(requires(_ASK, _TARGET))],
    cursor: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> QuestionPage:
    state = get_state()
    page_size = limit or settings.history_page_size
    async with user_connection(state.app_pool, tenant_id=claims.tid, user_sub=claims.sub) as conn:
        rows = await pg.list_questions(conn, tenant_id=claims.tid, limit=page_size, before=cursor)
    items = [
        QuestionView(
            id=row.id,
            text=row.text,
            mode=row.mode,
            locale=row.locale,
            created_at=row.created_at,
            answer_id=row.answer_id,
            answer_status=row.answer_status,
        )
        for row in rows
    ]
    # A short page is the end of the list; a full page may or may not be, and
    # the client finds out by asking once more (cheap, and avoids a count).
    next_cursor = items[-1].created_at if len(items) == page_size else None
    return QuestionPage(items=items, next_cursor=next_cursor)
