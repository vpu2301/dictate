"""GET /reports/search — day-6."""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import search as searchmod
from ..domain.pii_redactor import is_treatment_team, redact_snippet

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


class SearchHitDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    code: str
    title: str
    status: str
    encounter_date: str | None
    primary_author_id: UUID
    co_author_ids: list[UUID]
    icd10_codes: list[str]
    snippet: str
    updated_at: str


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hits: list[SearchHitDTO]
    next_cursor: str | None
    total_estimated: int | None
    total_exact: int | None = None


@router.get("/search", response_model=SearchResponse)
async def search_reports(
    claims: Annotated[Claims, Depends(requires("report.read", "report"))],
    q: str | None = Query(default=None, max_length=200),
    patient_id: UUID | None = None,
    author_id: UUID | None = None,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    encounter_date_from: date | None = None,
    encounter_date_to: date | None = None,
    icd10: list[str] | None = Query(default=None),
    cursor: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    total: str | None = Query(default=None, description="set to 'exact' for full count"),
) -> SearchResponse:
    state = get_state()
    cursor_decoded: tuple[date | None, UUID] | None = None
    if cursor:
        try:
            cursor_decoded = searchmod.decode_cursor(cursor)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid cursor: {exc}",
            ) from exc

    filters = searchmod.SearchFilters(
        q=q,
        patient_id=patient_id,
        author_id=author_id,
        statuses=status_filter,
        encounter_date_from=encounter_date_from,
        encounter_date_to=encounter_date_to,
        icd10=icd10,
    )

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        hits, next_cursor, total_estimated = await searchmod.search_reports(
            conn,
            filters=filters,
            limit=limit,
            cursor=cursor_decoded,
        )
        total_exact: int | None = None
        if total == "exact":
            total_exact = await searchmod.exact_total(conn, filters)

    out: list[SearchHitDTO] = []
    for h in hits:
        on_team = is_treatment_team(
            viewer_user_id=claims.sub,
            primary_author_id=h.primary_author_id,
            co_author_ids=h.co_author_ids,
            viewer_roles=list(claims.roles),
        )
        snippet = h.snippet if on_team else redact_snippet(h.snippet)
        out.append(
            SearchHitDTO(
                report_id=h.report_id,
                code=h.code,
                title=h.title,
                status=h.status,
                encounter_date=h.encounter_date.isoformat() if h.encounter_date else None,
                primary_author_id=h.primary_author_id,
                co_author_ids=h.co_author_ids,
                icd10_codes=h.icd10_codes,
                snippet=snippet,
                updated_at=h.updated_at.isoformat(),
            )
        )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_SEARCHED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=None,
        payload={
            "q": q or "",
            "has_q": q is not None,
            "result_count": len(out),
            "filters": {
                "status": status_filter or [],
                "icd10": icd10 or [],
                "encounter_date_from": encounter_date_from.isoformat()
                if encounter_date_from
                else None,
                "encounter_date_to": encounter_date_to.isoformat() if encounter_date_to else None,
            },
        },
        severity=Severity.INFO,
    )

    return SearchResponse(
        hits=out,
        next_cursor=next_cursor,
        total_estimated=total_estimated,
        total_exact=total_exact,
    )
