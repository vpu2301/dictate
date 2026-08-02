"""GET /reports/search — the report list surface.

Two standings reach it (S14):

  `report.read`  — clinician / nurse. The clinical list, and since S14 it
                   carries the patient's REAL name, resolved live from
                   `patients`, not the initials frozen into
                   `reports.patient_name_redacted` at creation time.
  `stats.read`   — tenant_admin, who holds no clinical read. Same rows,
                   stripped to counts and timings: no title, no snippet,
                   no patient reference of any kind. This is what keeps
                   the business dashboard's KPIs working after the admin
                   was separated from PHI, without giving back a
                   browsable list of patients' reports.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict

from audit import Severity
from auth import Claims, can_claims
from db import tenant_connection

from .. import audit_kinds
from ..deps import get_state, requires_any
from ..domain import query_expansion
from ..domain import reports_repository as repo
from ..domain import search as searchmod
from ..domain.pii_redactor import is_treatment_team, redact_snippet

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])

_meter = metrics.get_meter("mdx.report")
# Sprint 15: closes the sprint-08 gap — the dashboard panel "Search latency
# (split by has_q)" and the ReportSearchLatencyHigh alert have queried
# mdx_reports_search_latency_ms_histogram since sprint 08, but nothing ever
# created the instrument. unit deliberately empty (exporter appends unit
# names; values are ms); the "*latency*" View supplies the ms buckets.
_search_latency = _meter.create_histogram(
    "mdx_reports_search_latency_ms_histogram",
    description="End-to-end report search latency in ms (label has_q)",
    unit="",
)
_expansion_metric = _meter.create_counter(
    "mdx_reports_search_expansion_total",
    description="Query-expansion outcomes on /v1/reports/search (label hit)",
    unit="1",
)


class LocalizedName(BaseModel):
    """Bilingual patient name, matching core-service's `PatientOut.name`
    so the SPA can reuse one `displayName(patient, lang)` helper across
    the roster, the notes feed and the report list."""

    model_config = ConfigDict(extra="forbid")

    uk: str
    en: str


class SearchHitDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    code: str
    title: str
    status: str
    template_id: UUID
    encounter_date: str | None
    primary_author_id: UUID
    co_author_ids: list[UUID]
    patient_id: UUID | None
    patient_name_redacted: str | None
    # S14: the patient's real name, resolved live from `patients` for
    # callers holding clinical read (clinician / nurse). `None` for a
    # stats-mode caller, for a report with no patient, and for a patient
    # RLS will not show — so a client must always fall back to the
    # initials rather than assume this is populated.
    patient_name: LocalizedName | None = None
    icd10_codes: list[str]
    snippet: str
    updated_at: str


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hits: list[SearchHitDTO]
    next_cursor: str | None
    total_estimated: int | None
    total_exact: int | None = None
    # Sprint 15 (ADR-0038): synonym terms that broadened this query —
    # transparency for the FE ("also matching: інфаркт міокарда, MI").
    # Empty when expansion found nothing or expand=false.
    expanded_terms: list[str] = []


@router.get("/search", response_model=SearchResponse)
async def search_reports(
    claims: Annotated[
        Claims,
        Depends(requires_any(("report.read", "report"), ("stats.read", "tenant"))),
    ],
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
    expand: bool = Query(
        default=True,
        description="Synonym query expansion (ADR-0038); false = exact terms only.",
    ),
) -> SearchResponse:
    state = get_state()
    started = time.perf_counter()
    cursor_decoded: tuple[datetime, UUID] | None = None
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

    expanded_terms: list[str] = []
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if q and expand:
            expansion = await query_expansion.expand_query(conn, raw_q=q)
            if expansion.tsquery is not None and expansion.groups_used > 0:
                filters.ts_query = expansion.tsquery
                expanded_terms = expansion.expanded_terms
            _expansion_metric.add(
                1, {"hit": str(bool(expansion.groups_used)).lower()}
            )
        hits, next_cursor, total_estimated = await searchmod.search_reports(
            conn,
            filters=filters,
            limit=limit,
            cursor=cursor_decoded,
        )
        total_exact: int | None = None
        if total == "exact":
            total_exact = await searchmod.exact_total(conn, filters)

        # S14 — which of the two standings admitted this caller decides
        # what the rows may contain. `stats.read` alone (a tenant_admin)
        # gets counts and timings; `report.read` gets the clinical list.
        clinical = can_claims(claims, "report.read", "report")

        # Real names for the clinical list, resolved live rather than read
        # from the frozen `patient_name_redacted` initials. One query for
        # the page, inside the same RLS-scoped connection.
        patient_labels: dict[UUID, object] = {}
        if clinical:
            patient_labels = await repo.fetch_patient_labels(
                conn, patient_ids=[h.patient_id for h in hits if h.patient_id]
            )

    out: list[SearchHitDTO] = []
    for h in hits:
        if not clinical:
            # Stats mode. Every PHI-bearing field is dropped at
            # construction rather than blanked afterwards, so a field
            # added to SearchHitDTO later cannot leak by being forgotten
            # here — it simply takes its model default.
            out.append(
                SearchHitDTO(
                    report_id=h.report_id,
                    code=h.code,
                    title="",
                    status=h.status,
                    template_id=h.template_id,
                    encounter_date=h.encounter_date.isoformat() if h.encounter_date else None,
                    primary_author_id=h.primary_author_id,
                    co_author_ids=h.co_author_ids,
                    patient_id=None,
                    patient_name_redacted=None,
                    patient_name=None,
                    icd10_codes=[],
                    snippet="",
                    updated_at=h.updated_at.isoformat(),
                )
            )
            continue

        on_team = is_treatment_team(
            viewer_user_id=claims.sub,
            primary_author_id=h.primary_author_id,
            co_author_ids=h.co_author_ids,
            viewer_roles=list(claims.roles),
        )
        snippet = h.snippet if on_team else redact_snippet(h.snippet)
        label = patient_labels.get(h.patient_id) if h.patient_id else None
        out.append(
            SearchHitDTO(
                report_id=h.report_id,
                code=h.code,
                title=h.title,
                status=h.status,
                template_id=h.template_id,
                encounter_date=h.encounter_date.isoformat() if h.encounter_date else None,
                primary_author_id=h.primary_author_id,
                co_author_ids=h.co_author_ids,
                patient_id=h.patient_id,
                patient_name_redacted=h.patient_name_redacted,
                patient_name=(
                    LocalizedName(uk=label.name_uk, en=label.name_en)  # type: ignore[attr-defined]
                    if label is not None
                    else None
                ),
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
    if expanded_terms:
        # Aggregated search.expanded (ADR-0038): counted in memory, one
        # audit row per tenant per flush — never per keystroke.
        await state.search_audit_buffer.record(
            tenant_id=claims.tid, expanded_terms=len(expanded_terms)
        )

    _search_latency.record(
        (time.perf_counter() - started) * 1000.0,
        {"has_q": str(q is not None).lower()},
    )
    return SearchResponse(
        hits=out,
        next_cursor=next_cursor,
        total_estimated=total_estimated,
        total_exact=total_exact,
        expanded_terms=expanded_terms,
    )
