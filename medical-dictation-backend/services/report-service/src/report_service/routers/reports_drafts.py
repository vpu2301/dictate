"""PUT /reports/{id}/draft — autosave with optimistic locking."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field

from auth import Claims
from db import tenant_connection
from report_models import ReportContent, ReportStatus

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import reports_repository as repo
from ..domain.conflicts import OptimisticLockMismatchError
from ..domain.content_metadata import template_field_types
from ..domain.diff_engine import compute_diff, section_diff_summary
from ..domain.field_audit import diff_field_events
from ._content_guard import ensure_valid_field_metadata

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])

# Sprint-13 extraction-quality signal. Grafana reads these directly so the
# override-rate panel never has to scrape the audit table.
#
# LABEL DISCIPLINE: field_type ONLY. Option values are template-authored,
# so putting them in a label would make cardinality unbounded and put
# clinical vocabulary into the metrics store. Option-level analysis reads
# the audit payloads offline.
_meter = metrics.get_meter("mdx.anamnesis")
_confirmed = _meter.create_counter(
    "mdx_field_confirmed_total",
    unit="1",
    description="Clinician confirmed an extracted typed-field value",
)
_overridden = _meter.create_counter(
    "mdx_field_overridden_total",
    unit="1",
    description="Clinician REPLACED an extracted value — the extractor was wrong",
)


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
    responses={
        422: {
            "description": "Sprint-13 field-metadata validation failed: "
            "`field_metadata_invalid` (unknown keys / wrong shape / metadata on a "
            "field type that accepts none) or `choice_value_unknown` (a `selected` "
            "value not among the section's template options). Section-addressed "
            "problems in `problems[]`."
        }
    },
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

        # Sprint-13: typed field metadata must be valid at every write.
        await ensure_valid_field_metadata(conn, content=body.content)
        field_types = await template_field_types(conn, content=body.content)

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

    # Sprint-13: confirm/override signals — the extractor-quality loop.
    # Emitted per event (they are rare and clinician-initiated), unlike
    # the aggregated autosave row below. Payloads are slug/code only.
    for event in diff_field_events(
        before=current.content if current is not None else None,
        after=body.content,
        field_types=field_types,
    ):
        counter = _confirmed if event.kind == "confirmed" else _overridden
        counter.add(1, {"field_type": event.field_type})
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind=(
                audit_kinds.ANAMNESIS_FIELD_CONFIRMED
                if event.kind == "confirmed"
                else audit_kinds.ANAMNESIS_FIELD_OVERRIDDEN
            ),
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="report",
            target_id=report_id,
            payload={
                "section_key": event.section_key,
                "field_type": event.field_type,
                **event.payload,
            },
        )

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
