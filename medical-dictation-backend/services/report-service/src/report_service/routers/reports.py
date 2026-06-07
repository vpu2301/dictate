"""POST /reports + GET /reports/{id} — sprint-08 day-1/day-6."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Action, Claims, TargetKind
from db import tenant_connection
from report_models import ReadPurpose, ReportContent

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import code_sequence
from ..domain import reports_repository as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


# ── Request / response shapes ───────────────────────────────────────


class CreateReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: ReportContent
    patient_id: UUID | None = None
    co_author_ids: list[UUID] = Field(default_factory=list)
    source_session_id: UUID | None = None


class ReportCreatedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    code: str
    version_id: UUID
    version_number: int
    status: str


class ReportEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    code: str
    status: str
    current_version_id: UUID
    current_version_number: int
    primary_author_id: UUID
    co_author_ids: list[UUID]
    title: str
    icd10_codes: list[str]
    encounter_date: str | None
    created_at: str
    updated_at: str
    finalized_at: str | None
    signed_at: str | None
    cancelled_at: str | None
    content: ReportContent | None = None


# ── Helpers ─────────────────────────────────────────────────────────


def _envelope(row: repo.ReportRow, *, content: ReportContent | None = None) -> ReportEnvelope:
    return ReportEnvelope(
        id=row.id,
        code=row.code,
        status=row.status.value,
        current_version_id=row.current_version_id,
        current_version_number=row.current_version_number,
        primary_author_id=row.primary_author_id,
        co_author_ids=row.co_author_ids,
        title=row.title,
        icd10_codes=row.icd10_codes,
        encounter_date=row.encounter_date.isoformat() if row.encounter_date else None,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
        finalized_at=row.finalized_at.isoformat() if row.finalized_at else None,
        signed_at=row.signed_at.isoformat() if row.signed_at else None,
        cancelled_at=row.cancelled_at.isoformat() if row.cancelled_at else None,
        content=content,
    )


# ── Routes ──────────────────────────────────────────────────────────


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ReportCreatedResponse,
)
async def create_report(
    body: CreateReportRequest,
    claims: Annotated[Claims, Depends(requires(Action.WRITE, TargetKind.REPORT))],
) -> ReportCreatedResponse:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        code = await code_sequence.next_code(conn, tenant_id=claims.tid)
        report_id, version_id = await repo.create_report_with_v1(
            conn,
            tenant_id=claims.tid,
            code=code,
            primary_author_id=claims.sub,
            co_author_ids=body.co_author_ids,
            patient_id=body.patient_id,
            template_id=body.content.template_id,
            template_schema_version=body.content.template_schema_version,
            source_session_id=body.source_session_id,
            content=body.content,
        )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_CREATED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={"code": code, "version_id": str(version_id)},
        severity=Severity.INFO,
    )

    return ReportCreatedResponse(
        id=report_id,
        code=code,
        version_id=version_id,
        version_number=1,
        status="draft",
    )


@router.get("/{report_id}", response_model=ReportEnvelope)
async def get_report(
    report_id: UUID,
    claims: Annotated[Claims, Depends(requires(Action.READ, TargetKind.REPORT))],
    purpose: Annotated[
        ReadPurpose | None,
        Query(description="Required for non-author reads."),
    ] = None,
    include_content: bool = Query(default=True),
) -> ReportEnvelope:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.fetch_report(conn, report_id=report_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")

        # Read-purpose enforcement: required if requester is not author/co-author.
        is_author = claims.sub == row.primary_author_id or claims.sub in row.co_author_ids
        if not is_author and purpose is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "type": "https://errors.medical-dictation/missing-read-purpose",
                    "title": "Read purpose required",
                    "detail": "Non-author reads must include ?purpose=<value>",
                    "allowed": [p.value for p in ReadPurpose],
                },
            )

        content_obj: ReportContent | None = None
        if include_content:
            v = await repo.fetch_version(conn, version_id=row.current_version_id)
            content_obj = v.content if v else None

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_VIEWED_FULL,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={
            "purpose": purpose.value if purpose else "author",
            "is_author": is_author,
        },
        severity=Severity.INFO,
    )

    return _envelope(row, content=content_obj)
