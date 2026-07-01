"""GET /reports/{id}/pdf — server-rendered PDF (M1·A3 + draft export).

Renders the current version of a report as a PDF. Non-signed reports
(draft / finalized / amended) are rendered with a visible DRAFT treatment
(watermark + banner + "no legal force" disclaimer) so an unsigned export
is never mistaken for a legally-binding document. Only a *cancelled*
report is refused (409); a signed report can be exported "clean" via
``?variant=clean``. The weasyprint import lives behind ``domain.pdf`` so
it never loads on the router import path.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal
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
from ..domain.branding import load_tenant_branding
from ..domain.pdf import render_report_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


@router.get(
    "/{report_id}/pdf",
    summary="Render the current version as a PDF (draft watermark for non-signed reports; 409 only for cancelled).",
    responses={200: {"content": {"application/pdf": {}}}},
)
async def get_report_pdf(
    report_id: UUID,
    claims: Annotated[Claims, Depends(requires("report.read", "report"))],
    purpose: Annotated[
        ReadPurpose | None, Query(description="Required for non-author reads.")
    ] = None,
    variant: Annotated[
        Literal["draft", "clean"],
        Query(description="'clean' is only honoured for signed reports; non-signed reports are always draft."),
    ] = "draft",
    lang: Annotated[
        Literal["uk", "en"] | None,
        Query(description="Render language; falls back to the report language, else 'uk'."),
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

        # A cancelled report must never be exported.
        if report.status == ReportStatus.CANCELLED:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "type": "https://errors.medical-dictation/report-cancelled",
                    "title": "Report is cancelled",
                    "status": status.HTTP_409_CONFLICT,
                    "detail": (
                        f"report {report_id} is cancelled and cannot be rendered to PDF"
                    ),
                    "report_status": report.status.value,
                },
            )

        version = await repo.fetch_version(conn, version_id=report.current_version_id)
        if version is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="version not found")

        # Tenant branding for the document header (issuer name). Read under the
        # same RLS-scoped connection; falls back to the configured default when
        # the tenant carries no branding.
        branding = await load_tenant_branding(conn, tenant_id=str(claims.tid))

    # Draft treatment whenever the report is not signed, OR when explicitly
    # requested via ``variant=draft``. ``clean`` is only honoured for signed
    # reports; any non-signed report is forced to draft regardless of variant.
    is_signed = report.status == ReportStatus.SIGNED
    is_draft = (not is_signed) or variant == "draft"
    language = lang or "uk"

    # Prefer the tenant's registered/legal name as the document issuer; fall
    # back to the service-level default when the tenant has no branding set.
    issuer_name = branding.issuer_name if branding.issuer_name != "—" else settings.pdf_issuer_name
    pdf_bytes = render_report_pdf(
        report=report,
        version=version,
        issuer_name=issuer_name,
        is_draft=is_draft,
        language=language,
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
            "variant": "draft" if is_draft else "clean",
            "report_status": report.status.value,
        },
        severity=Severity.INFO,
    )

    filename = f"report-{report_id}-draft.pdf" if is_draft else f"report-{report_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
