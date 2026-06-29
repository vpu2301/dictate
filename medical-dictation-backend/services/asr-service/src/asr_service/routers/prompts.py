"""``GET /asr/prompts`` — list the medical-prompt catalogue (M1·C1).

Read-only. Source is the global ``medical_prompts`` table (migration 0008,
no RLS per ADR-0007) so the picker lists exactly the UUIDs ``submit_job``
stores. A plain ``tenant_connection`` is still used for the scoped role.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from auth import Claims
from db import tenant_connection

from ..deps import get_state, requires
from ..domain import repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/asr", tags=["asr"])


class PromptSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    language: str
    specialty: str
    is_default: bool


@router.get(
    "/prompts",
    response_model=list[PromptSummary],
    summary="List the medical-prompt catalogue (optionally filtered).",
)
async def list_prompts(
    claims: Annotated[Claims, Depends(requires("asr.read", "asr_job"))],
    language: Annotated[str | None, Query(pattern=r"^(uk|en)$")] = None,
    specialty: Annotated[str | None, Query()] = None,
) -> list[PromptSummary]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await repository.list_prompts(conn, language=language, specialty=specialty)
    return [
        PromptSummary(
            id=r.id,
            language=r.language,
            specialty=r.specialty,
            is_default=r.is_default,
        )
        for r in rows
    ]
