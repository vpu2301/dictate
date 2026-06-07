"""POST /reports/{id}/amend — sprint-08 day-4.

Sprint-08 ships the amendment-drafting path. Status transitions to
``amended`` happen in sprint-09 when the amendment is signed. Until
then the amendment lives as a new ``report_versions`` row with
``is_amendment=true`` and the report stays in ``signed``.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Action, Claims, TargetKind
from db import tenant_connection
from report_models import ReportAmendmentType, ReportContent, ReportStatus

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import reports_repository as repo
from ..domain.diff_engine import compute_diff, section_diff_summary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


class AmendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amendment_type: ReportAmendmentType
    amendment_reason: str = Field(min_length=1, max_length=4000)
    content: ReportContent


class AmendResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: UUID
    version_number: int
    parent_version_id: UUID
    is_amendment: bool
    amendment_type: ReportAmendmentType
    report_status: str
    diff_summary: dict[str, list[str]]


@router.post("/{report_id}/amend", response_model=AmendResponse)
async def amend_report(
    report_id: UUID,
    body: AmendRequest,
    claims: Annotated[Claims, Depends(requires(Action.WRITE, TargetKind.REPORT))],
) -> AmendResponse:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.lock_report_for_update(conn, report_id=report_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
        if row.status != ReportStatus.SIGNED:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "amend_requires_signed",
                    "current_status": row.status.value,
                },
            )

        current = await repo.fetch_version(conn, version_id=row.current_version_id)
        assert current is not None

        diff = compute_diff(
            report_id=str(report_id),
            from_version_id=str(current.id),
            from_version_number=current.version_number,
            from_content=current.content,
            to_version_id="pending",
            to_version_number=current.version_number + 1,
            to_content=body.content,
        )

        new_id, new_n = await repo.append_version(
            conn,
            report_id=report_id,
            expected_version=row.current_version_number,
            new_content=body.content,
            created_by=claims.sub,
            diff_jsonb=section_diff_summary(diff),
            is_amendment=True,
            amendment_type=body.amendment_type,
            amendment_reason=body.amendment_reason,
            parent_version_id_override=current.id,
        )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_AMENDMENT_DRAFTED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={
            "version_number": new_n,
            "amendment_type": body.amendment_type.value,
            "parent_version_id": str(current.id),
        },
        severity=Severity.INFO,
    )

    return AmendResponse(
        version_id=new_id,
        version_number=new_n,
        parent_version_id=current.id,
        is_amendment=True,
        amendment_type=body.amendment_type,
        # Status stays SIGNED until sprint-09 attaches signing.
        report_status=ReportStatus.SIGNED.value,
        diff_summary=section_diff_summary(diff),
    )


@router.post(
    "/{report_id}/sign",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def sign_placeholder(
    report_id: UUID,
    claims: Annotated[Claims, Depends(requires(Action.WRITE, TargetKind.REPORT))],
) -> dict[str, str]:
    """Sprint-09 implements; sprint-08 leaves the route shape locked."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="signing is implemented in sprint-09 (KEP)",
    )
