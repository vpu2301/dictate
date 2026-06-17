"""PUT /reports/{id}/draft — autosave with optimistic locking."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from auth import Claims
from db import tenant_connection
from report_models import ReportContent, ReportStatus

from ..deps import get_state, requires
from ..domain import reports_repository as repo
from ..domain.conflicts import OptimisticLockMismatchError
from ..domain.diff_engine import compute_diff, section_diff_summary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


class UpdateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    content: ReportContent
    dictation_session_id: UUID | None = None


class UpdateDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: UUID
    version_number: int
    status: str
    diff_summary: dict[str, list[str]]
    idempotent_replay: bool = False


@router.put(
    "/{report_id}/draft",
    response_model=UpdateDraftResponse,
)
async def update_draft(
    report_id: UUID,
    body: UpdateDraftRequest,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> UpdateDraftResponse:
    state = get_state()

    allowed, retry_after = await state.autosave_rate_limiter.check_and_record(report_id=report_id)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
            detail={"error": "autosave_rate_limited", "retry_after": retry_after},
        )

    body_hash = repo.body_hash_for(body.content)

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.lock_report_for_update(conn, report_id=report_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
        if row.status != ReportStatus.DRAFT:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "wrong_status_for_draft_update",
                    "current_status": row.status.value,
                },
            )

        # Idempotency: same body_hash as most recent version + expected_version
        # matches current → return prior version, no new row.
        if body.expected_version == row.current_version_number:
            current = await repo.fetch_version(conn, version_id=row.current_version_id)
            if current is not None and current.body_hash == body_hash:
                logger.info(
                    "draft.update.idempotent_replay",
                    extra={
                        "report_id": str(report_id),
                        "version_number": current.version_number,
                    },
                )
                return UpdateDraftResponse(
                    version_id=current.id,
                    version_number=current.version_number,
                    status=row.status.value,
                    diff_summary={"added": [], "removed": [], "modified": []},
                    idempotent_replay=True,
                )

        try:
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
                expected_version=body.expected_version,
                new_content=body.content,
                created_by=claims.sub,
                diff_jsonb=section_diff_summary(diff),
                body_hash_override=body_hash,
            )
        except OptimisticLockMismatchError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "optimistic_lock_mismatch",
                    "current_version": exc.current_version,
                    "expected_version": exc.expected_version,
                },
            ) from exc

    # Aggregated audit (per dictation session, not per autosave).
    await state.draft_audit_buffer.record(
        tenant_id=claims.tid,
        report_id=report_id,
        dictation_session_id=body.dictation_session_id,
        actor_user_id=claims.sub,
        version_number=new_n,
    )

    return UpdateDraftResponse(
        version_id=new_id,
        version_number=new_n,
        status=ReportStatus.DRAFT.value,
        diff_summary=section_diff_summary(diff),
        idempotent_replay=False,
    )
