"""GET /reports/{id}/versions[/{v}] — version history (M1·A1/A2).

Read-only. Mirrors ``reports_diff.py``: 404-first on the report, the same
non-author ``?purpose=`` enforcement ``get_report`` applies, and a
tenant-scoped connection so RLS does the isolation.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from audit import Severity
from auth import Claims
from db import tenant_connection
from report_models import ReadPurpose, ReportAmendmentType, ReportContent

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import reports_repository as repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


# ── Wire models ─────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportVersionSummary(_Strict):
    id: UUID
    version_number: int
    parent_version_id: UUID | None
    created_by: UUID
    created_at: str
    is_amendment: bool
    amendment_type: ReportAmendmentType | None
    amendment_reason: str | None
    signed_at: str | None
    signed_by: UUID | None


class ReportVersionDetail(ReportVersionSummary):
    content: ReportContent
    rendered_text: str


def _enforce_read_purpose(report: repo.ReportRow, claims: Claims, purpose: ReadPurpose | None) -> bool:
    """Returns ``is_author``; raises 422 when a non-author omits ``?purpose=``."""
    is_author = claims.sub == report.primary_author_id or claims.sub in report.co_author_ids
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
    return is_author


@router.get("/{report_id}/versions", response_model=list[ReportVersionSummary])
async def list_versions(
    report_id: UUID,
    claims: Annotated[Claims, Depends(requires("report.read", "report"))],
    purpose: Annotated[
        ReadPurpose | None, Query(description="Required for non-author reads.")
    ] = None,
) -> list[ReportVersionSummary]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        report = await repo.fetch_report(conn, report_id=report_id)
        if report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
        _enforce_read_purpose(report, claims, purpose)
        summaries = await repo.list_version_summaries(conn, report_id=report_id)

    return [
        ReportVersionSummary(
            id=s.id,
            version_number=s.version_number,
            parent_version_id=s.parent_version_id,
            created_by=s.created_by,
            created_at=s.created_at.isoformat(),
            is_amendment=s.is_amendment,
            amendment_type=s.amendment_type,
            amendment_reason=s.amendment_reason,
            signed_at=s.signed_at.isoformat() if s.signed_at else None,
            signed_by=s.signed_by,
        )
        for s in summaries
    ]


@router.get("/{report_id}/versions/{version_number}", response_model=ReportVersionDetail)
async def get_version(
    report_id: UUID,
    version_number: int,
    claims: Annotated[Claims, Depends(requires("report.read", "report"))],
    purpose: Annotated[
        ReadPurpose | None, Query(description="Required for non-author reads.")
    ] = None,
) -> ReportVersionDetail:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        report = await repo.fetch_report(conn, report_id=report_id)
        if report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
        is_author = _enforce_read_purpose(report, claims, purpose)
        version = await repo.fetch_version_by_number(
            conn, report_id=report_id, version_number=version_number
        )
        if version is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="version not found")

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_VIEWED_FULL,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={
            "version_number": version_number,
            "purpose": purpose.value if purpose else "author",
            "is_author": is_author,
        },
        severity=Severity.INFO,
    )

    return ReportVersionDetail(
        id=version.id,
        version_number=version.version_number,
        parent_version_id=version.parent_version_id,
        created_by=version.created_by,
        created_at=version.created_at.isoformat(),
        is_amendment=version.is_amendment,
        amendment_type=version.amendment_type,
        amendment_reason=version.amendment_reason,
        signed_at=version.signed_at.isoformat() if version.signed_at else None,
        signed_by=version.signed_by,
        content=version.content,
        rendered_text=version.rendered_text,
    )
