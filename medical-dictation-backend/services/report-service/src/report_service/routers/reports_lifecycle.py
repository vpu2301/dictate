"""Finalize / revert / cancel routes."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Action, Claims, TargetKind
from db import tenant_connection

from report_models import ReportStatus

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import reports_repository as repo
from ..domain.report_lifecycle import (
    ConcurrentTransitionError,
    IllegalTransitionError,
    NotPrimaryAuthorError,
    ReportStateMachine,
    RevertWindowExceededError,
    TransitionAction,
)
from ..domain.finalize_validator import validate_finalize

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


@router.post("/{report_id}/finalize", response_model=FinalizeResponse)
async def finalize_report(
    report_id: UUID,
    claims: Annotated[Claims, Depends(requires(Action.WRITE, TargetKind.REPORT))],
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

        current = await repo.fetch_version(conn, version_id=row.current_version_id)
        assert current is not None

        # Load template to run finalize validation.
        template = await _fetch_template_definition(
            conn, template_id=current.content.template_id
        )
        problems = validate_finalize(content=current.content, template=template)
        if problems:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "finalize_validation_failed",
                    "problems": [p.as_dict() for p in problems],
                },
            )

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
    return FinalizeResponse(id=report_id, status=ReportStatus.FINALIZED.value)


@router.post("/{report_id}/revert-to-draft", response_model=FinalizeResponse)
async def revert_to_draft(
    report_id: UUID,
    claims: Annotated[Claims, Depends(requires(Action.WRITE, TargetKind.REPORT))],
) -> FinalizeResponse:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.lock_report_for_update(conn, report_id=report_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
        try:
            await _sm.revert_to_draft(
                conn, report_id=report_id, actor_user_id=claims.sub
            )
        except IllegalTransitionError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
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
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc

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
    claims: Annotated[Claims, Depends(requires(Action.WRITE, TargetKind.REPORT))],
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
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        except ConcurrentTransitionError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc

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
    from ..domain.repository import get_template  # type: ignore
    from template_models import TemplateDefinition
    import json

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
