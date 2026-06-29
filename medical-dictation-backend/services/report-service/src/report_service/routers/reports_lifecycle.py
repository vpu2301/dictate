"""Finalize / revert / cancel routes."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection
from report_models import ReportStatus

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import reports_repository as repo
from ..domain.finalize_validator import validate_finalize
from ..domain.report_lifecycle import (
    ConcurrentTransitionError,
    IllegalTransitionError,
    NotPrimaryAuthorError,
    ReportStateMachine,
    RevertWindowExceededError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])

_sm = ReportStateMachine()


class FinalizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: str


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


class FinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional optimistic-lock guard: when present it must equal the
    # report's current version number, else 409. Absent → fall back to the
    # status-based lock alone (backward compatible / no-body finalize).
    expected_version: int | None = Field(default=None, ge=1)
    # Links the originating dictation session to the report when not
    # already set at create time (Item 5).
    dictation_session_id: UUID | None = None


@router.post("/{report_id}/finalize", response_model=FinalizeResponse)
async def finalize_report(
    report_id: UUID,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
    body: FinalizeRequest | None = None,
) -> FinalizeResponse:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.lock_report_for_update(conn, report_id=report_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
        if row.status != ReportStatus.DRAFT:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "wrong_status_for_finalize",
                    "current_status": row.status.value,
                },
            )

        # Optional optimistic-lock guard (in addition to the status lock).
        if (
            body is not None
            and body.expected_version is not None
            and body.expected_version != row.current_version_number
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "optimistic_lock_mismatch",
                    "current_version": row.current_version_number,
                    "expected_version": body.expected_version,
                },
            )

        current = await repo.fetch_version(conn, version_id=row.current_version_id)
        assert current is not None

        # Load template to run finalize validation.
        template = await _fetch_template_definition(conn, template_id=current.content.template_id)
        problems = validate_finalize(content=current.content, template=template)
        if problems:
            # Surface the per-section problems as first-class RFC-9457 extension
            # members (not stuffed into `detail`, which the global handler
            # str()-wraps) so the SPA can render field-level errors from JSON.
            exc = HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Report failed finalize validation.",
            )
            exc.problem_extras = {  # type: ignore[attr-defined]
                "code": "finalize_validation_failed",
                "problems": [p.as_dict() for p in problems],
            }
            raise exc

        # Session → report linkage (Item 5): backfill only when absent.
        source_session_id = row.source_session_id
        if (
            body is not None
            and body.dictation_session_id is not None
            and row.source_session_id is None
        ):
            await repo.set_source_session_id_if_absent(
                conn, report_id=report_id, session_id=body.dictation_session_id
            )
            source_session_id = body.dictation_session_id

        try:
            await _sm.finalize(conn, report_id=report_id)
        except ConcurrentTransitionError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "concurrent_transition",
                    "current_status": exc.observed_status.value if exc.observed_status else None,
                },
            ) from exc
        except IllegalTransitionError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    sections = current.content.sections
    low_confidence_count = sum(1 for s in sections if "[[" in (s.text or ""))

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_FINALIZED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={"version_number": row.current_version_number},
        severity=Severity.INFO,
    )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_COMPLETED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=str(report_id),
        payload={
            "version_number": row.current_version_number,
            "section_count": len(sections),
            "low_confidence_count": low_confidence_count,
            "source_session_id": str(source_session_id) if source_session_id else None,
        },
        severity=Severity.INFO,
    )
    return FinalizeResponse(id=report_id, status=ReportStatus.FINALIZED.value)


@router.post("/{report_id}/revert-to-draft", response_model=FinalizeResponse)
async def revert_to_draft(
    report_id: UUID,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> FinalizeResponse:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.lock_report_for_update(conn, report_id=report_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
        try:
            await _sm.revert_to_draft(conn, report_id=report_id, actor_user_id=claims.sub)
        except IllegalTransitionError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except NotPrimaryAuthorError as exc:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="only the primary author may revert",
            ) from exc
        except RevertWindowExceededError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="revert window of 1 hour has elapsed",
            ) from exc
        except ConcurrentTransitionError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_REVERTED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={},
        severity=Severity.INFO,
    )
    return FinalizeResponse(id=report_id, status=ReportStatus.DRAFT.value)


@router.post("/{report_id}/cancel", response_model=FinalizeResponse)
async def cancel_report(
    report_id: UUID,
    body: CancelRequest,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> FinalizeResponse:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.lock_report_for_update(conn, report_id=report_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
        try:
            await _sm.cancel(
                conn,
                report_id=report_id,
                from_status=row.status,
                reason=body.reason,
            )
        except IllegalTransitionError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except ConcurrentTransitionError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_CANCELLED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={"reason": body.reason},
        severity=Severity.INFO,
    )
    return FinalizeResponse(id=report_id, status=ReportStatus.CANCELLED.value)


# ── Template fetch (lazy import to avoid circular) ──────────────────


async def _fetch_template_definition(conn, *, template_id: UUID):
    # report_service.domain.repository owns templates queries — we reuse
    # one of its helpers.
    import json

    from template_models import TemplateDefinition

    from ..domain.repository import get_template  # type: ignore

    row = await get_template(conn, template_id=template_id)
    if row is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="report references an unknown template",
        )
    raw = row["schema_jsonb"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    return TemplateDefinition.model_validate(raw)
