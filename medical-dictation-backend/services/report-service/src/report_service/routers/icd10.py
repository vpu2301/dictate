"""``GET /v1/icd10/search`` — МКХ-10 lookup for the diagnosis picker (sprint 13).

``icd10_codes`` is a global reference table with no tenant dimension
(no RLS, allowlisted in ``check-rls``), so this reads through the plain
app pool rather than ``tenant_connection`` — there is no tenant scope
to set. The ranking SQL is shared verbatim with the nlp extractor
(``db.ICD10_SEARCH_SQL``) so the picker and the extractor can never
disagree about the best match.

No per-request audit event: this sits in the picker's typing path and a
row per keystroke would be audit-chain pollution. Metrics only —
matching the sprint-10 autocomplete-suggest precedent (recorded as a
deviation in the sprint-13 sign-off).
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict

from auth import Claims
from db import ICD10_SEARCH_SQL

from ..deps import get_state, requires

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/icd10", tags=["icd10"])

_meter = metrics.get_meter("mdx.icd10")
_search_latency = _meter.create_histogram(
    "mdx_icd10_search_seconds",
    unit="s",
    description="ICD-10 search latency (picker typing path; budget p95 ≤ 50 ms)",
)
_searches = _meter.create_counter("mdx_icd10_searches_total", unit="1")


class Icd10SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    display: str  # display_uk — uk-first, mirrors report_models.Icd10Code.display
    is_leaf: bool


class Icd10SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    results: list[Icd10SearchHit]


@router.get(
    "/search",
    response_model=Icd10SearchResponse,
    summary="Search МКХ-10 codes — exact code first, then code prefix, then full text.",
    responses={422: {"description": "`q` outside 1..80 chars, or `limit` outside 1..20."}},
)
async def search_icd10(
    claims: Annotated[Claims, Depends(requires("report.read", "report"))],
    q: Annotated[str, Query(min_length=1, max_length=80)],
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> Icd10SearchResponse:
    state = get_state()
    started = time.perf_counter()
    async with state.app_pool.acquire() as conn:
        rows = await conn.fetch(ICD10_SEARCH_SQL, q, limit)
    elapsed = time.perf_counter() - started
    _search_latency.record(elapsed)
    _searches.add(1, {"has_results": str(bool(rows)).lower()})

    return Icd10SearchResponse(
        results=[
            Icd10SearchHit(code=r["code"], display=r["display_uk"], is_leaf=r["is_leaf"])
            for r in rows
        ]
    )
