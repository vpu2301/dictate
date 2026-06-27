"""GET /reports/{id}/pdf — unsigned PDF for local KEP (M1·A3).

Renders the current version of a *finalized* report as a PDF (the bytes a
clinician signs locally, then uploads via signing-service B4). Drafts get a
409 RFC-9457. The weasyprint import lives behind ``domain.pdf`` so it never
loads on the router import path.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from audit import Severity
from auth import Claims
from db import tenant_connection
from report_models import ReadPurpose, ReportStatus

from .. import audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain import reports_repository as repo
from ..domain.pdf import render_report_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


@router.get(
    "/{report_id}/pdf",
    summary="Render the current finalized version as an unsigned PDF (409 for drafts).",
    responses={200: {"content": {"application/pdf": {}}}},
)
async def get_report_pdf(
    report_id: UUID,
    claims: Annotated[Claims, Depends(requires("report.read", "report"))],
    purpose: Annotated[
        ReadPurpose | None, Query(description="Required for non-author reads.")
    ] = None,
) -> Response:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        report = await repo.fetch_report(conn, report_id=report_id)
        if report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")

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

        if report.status != ReportStatus.FINALIZED or report.finalized_at is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "type": "https://errors.medical-dictation/report-not-finalized",
                    "title": "Report is not finalized",
                    "status": status.HTTP_409_CONFLICT,
                    "detail": (
                        f"report {report_id} is in status {report.status.value!r}; "
                        "only finalized reports can be rendered to PDF"
                    ),
                    "report_status": report.status.value,
                },
            )

        version = await repo.fetch_version(conn, version_id=report.current_version_id)
        if version is None:  # pragma: no cover — finalized reports always have a version
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="version not found")

    pdf_bytes = render_report_pdf(
        report=report, version=version, issuer_name=settings.pdf_issuer_name
    )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_PDF_RENDERED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={
            "version_number": version.version_number,
            "size_bytes": len(pdf_bytes),
            "purpose": purpose.value if purpose else "author",
        },
        severity=Severity.INFO,
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{report.code}.pdf"'},
    )
