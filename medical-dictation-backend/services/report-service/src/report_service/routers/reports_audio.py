"""GET /v1/reports/{id}/sections/{section_key}/audio-clips — replay segments.

Lists the audio moments behind one report section so the clinician can
tap a sentence and hear the ground truth (sprint 15, ADR-0037).

Sections populated by the sprint-14 conversation draft carry
``transcript_segment_ids`` and map 1:1; everything older predates the
field (a sprint-08 placeholder) and falls back to the WHOLE session
transcript — replay still works, the FE aligns by timing. Timings +
speakers only, no transcript text: the text is already in the report the
caller just read.

Access: the same break-glass-aware gate as every single-report content
surface (``report_read_access``) plus the sprint-08 ``?purpose=`` rule
for non-authors.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from audit import Severity
from db import tenant_connection
from report_models import ReadPurpose

from .. import audit_kinds
from ..deps import get_state
from ..domain import audio_clips as clips
from ..domain import reports_repository as repo
from ._phi_access_guard import ReportReadAccess, report_read_access
from .reports_versions import _enforce_read_purpose

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


class AudioSegmentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_id: UUID | None  # conversation-mode segments only; else null
    index: int  # position in the session transcript — stable address
    start_ms: int
    end_ms: int
    speaker: str | None
    speaker_role: str | None


@router.get(
    "/{report_id}/sections/{section_key}/audio-clips",
    response_model=list[AudioSegmentOut],
)
async def list_section_audio_segments(
    report_id: UUID,
    section_key: str,
    access: Annotated[ReportReadAccess, Depends(report_read_access)],
    purpose: Annotated[
        ReadPurpose | None, Query(description="Required for non-author reads.")
    ] = None,
) -> list[AudioSegmentOut]:
    state = get_state()
    claims = access.claims
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        report = await repo.fetch_report(conn, report_id=report_id)
        if report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")
        _enforce_read_purpose(report, claims, purpose)

        if report.source_session_id is None:
            # Batch reports have no session transcript to list; clip
            # creation by explicit ms range still works (the FE holds the
            # batch transcript's timings).
            return []

        version = await repo.fetch_version(conn, version_id=report.current_version_id)
        if version is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="version not found")
        section = next(
            (s for s in version.content.sections if s.section_key == section_key), None
        )
        if section is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="section not found")

        transcript = await clips.load_session_transcript(
            conn, session_id=report.source_session_id
        )

    refs = clips.segments_from_transcript(
        transcript, segment_ids=section.transcript_segment_ids
    )
    if access.is_break_glass:
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind=audit_kinds.PHI_ACCESS_USED,
            actor_sub=claims.sub,
            actor_role=(claims.roles[0] if claims.roles else None),
            target_kind="report",
            target_id=report_id,
            payload={
                "grant_id": str(access.grant_id),
                "reason_code": access.reason_code,
                "surface": "audio_clip",
            },
            severity=Severity.SEC,
        )
    return [
        AudioSegmentOut(
            segment_id=r.segment_id,
            index=r.index,
            start_ms=r.start_ms,
            end_ms=r.end_ms,
            speaker=r.speaker,
            speaker_role=r.speaker_role,
        )
        for r in refs
    ]
