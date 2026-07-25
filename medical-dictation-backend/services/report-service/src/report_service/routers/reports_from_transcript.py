"""Assign a batch transcription to a patient as a dictation document.

``POST /v1/reports/from-transcript`` — fetch a COMPLETE asr job's
NLP-enriched transcript from asr-service (the caller's own bearer is
forwarded; asr-service authorizes + tenant-scopes the read and audits
it), pick a template (explicit ``template_id`` or deterministic
auto-match), and create a draft report whose first free-text section
holds the transcript. The report then appears in the patient timeline
like any other dictation document, and follows the normal report
lifecycle (edit → finalize → sign).

``GET /v1/reports/by-source-job`` — bulk lookup so the transcription
jobs list can badge jobs that are already assigned.

One report per source job per tenant is enforced by a partial unique
index (migration 0045); a concurrent double-assign surfaces as 409.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection
from report_models import ReportContent, ReportSection
from template_models import FieldType, TemplateDefinition

from .. import audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain import code_sequence, template_match
from ..domain import reports_repository as repo
from ..domain.field_extraction_client import extract_fields
from ..domain.pii_redactor import name_to_initials
from ..domain.repository import get_template

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])

_ASR_TIMEOUT = httpx.Timeout(connect=2.0, read=15.0, write=5.0, pool=5.0)


# ── Wire models ─────────────────────────────────────────────────────


class CreateFromTranscriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asr_job_id: UUID
    patient_id: UUID
    # Omitted → deterministic auto-match against the transcript.
    template_id: UUID | None = None
    title: str = Field(default="", max_length=512)
    encounter_date: str | None = None  # ISO-8601 date


class FromTranscriptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    code: str
    version_id: UUID
    version_number: int
    status: str
    patient_id: UUID
    template_id: UUID
    template_name: str
    template_selection: Literal["explicit", "auto", "fallback"]
    template_score: int | None = None


class SourceJobLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asr_job_id: UUID
    report_id: UUID
    code: str
    status: str
    patient_id: UUID | None


# ── asr-service fetch (mirrors the reports_sign signing call) ───────


async def _fetch_transcript(job_id: UUID, *, auth_header: str) -> dict[str, Any]:
    url = f"{settings.asr_service_base_url.rstrip('/')}/asr/jobs/{job_id}/result"
    try:
        async with httpx.AsyncClient(timeout=_ASR_TIMEOUT) as client:
            resp = await client.get(url, headers={"Authorization": auth_header})
    except httpx.HTTPError as exc:
        logger.warning("from_transcript.asr_unreachable: %s", exc.__class__.__name__)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "asr_service_unavailable"},
        ) from exc

    if resp.status_code == status.HTTP_200_OK:
        return resp.json()  # type: ignore[no-any-return]

    # Pass through the statuses that carry meaning for the caller:
    # 404 unknown job, 409 not complete yet, 410 transcript erased,
    # 403 the caller may not read that job.
    if resp.status_code in (403, 404, 409, 410):
        try:
            detail = resp.json().get("detail", resp.json())
        except Exception:  # noqa: BLE001
            detail = {"error": "transcript_unavailable"}
        raise HTTPException(resp.status_code, detail=detail)

    logger.warning("from_transcript.asr_unexpected_status: %s", resp.status_code)
    raise HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        detail={"error": "asr_service_error", "upstream_status": resp.status_code},
    )


# ── Content assembly ────────────────────────────────────────────────


def _transcript_text(result: dict[str, Any]) -> str:
    # Join sentences with a space, not a newline: the report editor
    # serialises its content in a way that drops newlines (turning
    # "...тижнів.\nРегіографія..." into glued "...тижнів.Регіографія..."),
    # so a space is the separator that survives a round-trip.
    parts = [str(seg.get("text", "")).strip() for seg in result.get("segments", [])]
    return " ".join(p for p in parts if p)


def _content_for_template(
    *,
    definition: TemplateDefinition,
    template_id: UUID,
    schema_version: int,
    transcript: str,
    title: str,
    encounter_date: str | None,
    extracted_fields: dict[str, dict[str, Any]] | None = None,
) -> ReportContent:
    """All template sections in order; the transcript lands in the first
    free-text section (dictations are linear speech — distributing text
    across sections is the clinician's edit, not a guess we make).

    Sprint 13: typed sections additionally carry the extractor's
    PROPOSALS in ``field_specific_metadata`` (``source: "extracted"``).
    The prose always stays intact in the free-text section — a proposal
    never consumes or rewrites what was dictated.
    """
    ordered = sorted(definition.sections, key=lambda s: s.order)
    target = next((s for s in ordered if s.field_type == FieldType.FREE_TEXT), ordered[0])
    proposals = extracted_fields or {}
    sections = [
        ReportSection(
            section_key=s.id,
            text=transcript if s.id == target.id else s.default_content,
            field_specific_metadata=proposals.get(s.id, {}),
        )
        for s in ordered
    ]
    return ReportContent(
        template_id=template_id,
        template_schema_version=schema_version,
        title=title,
        encounter_date=encounter_date,
        sections=sections,
    )


# ── Routes ──────────────────────────────────────────────────────────


@router.post(
    "/from-transcript",
    status_code=status.HTTP_201_CREATED,
    response_model=FromTranscriptResponse,
)
async def create_report_from_transcript(
    body: CreateFromTranscriptRequest,
    request: Request,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> FromTranscriptResponse:
    state = get_state()
    auth_header = request.headers.get("authorization") or ""

    result = await _fetch_transcript(body.asr_job_id, auth_header=auth_header)
    transcript = _transcript_text(result)
    if not transcript:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "empty_transcript", "detail": "the job's transcript is empty"},
        )
    language = str(result.get("language", "uk"))

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        existing = await conn.fetchrow(
            "SELECT id, code FROM reports WHERE source_asr_job_id = $1",
            body.asr_job_id,
        )
        if existing is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "already_assigned",
                    "detail": "this transcription is already assigned to a report",
                    "report_id": str(existing["id"]),
                    "report_code": existing["code"],
                },
            )

        # Same guard + 422 semantics as POST /v1/reports.
        patient = await repo.fetch_patient_label(conn, patient_id=body.patient_id)
        if patient is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "patient_not_found", "detail": "patient_not_found"},
            )
        patient_name_redacted = name_to_initials(patient.name_uk or patient.name_en)

        selection: str
        score: int | None = None
        if body.template_id is not None:
            row = await get_template(conn, template_id=body.template_id)
            if row is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"code": "template_not_found", "detail": "template_not_found"},
                )
            definition = _parse_definition(row)
            template_id, template_name = body.template_id, str(row["name"])
            schema_version = int(row["schema_version"])
            selection = "explicit"
        else:
            candidates = await template_match.load_candidates(conn, language=language)
            choice = template_match.select_template(candidates, transcript)
            if choice is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "code": "no_templates",
                        "detail": f"no active {language} templates to assign against",
                    },
                )
            definition = choice.candidate.definition
            template_id, template_name = choice.candidate.id, choice.candidate.name
            schema_version = choice.candidate.schema_version
            selection, score = choice.mode, choice.score

        title = body.title.strip() or (
            f"{template_name} — {body.encounter_date or date.today().isoformat()}"
        )
        # Sprint 13 (ADR-0028): typed-field proposals. Fail-open — an
        # unreachable nlp-service costs proposals, not the draft.
        extracted_fields = await extract_fields(
            definition=definition,
            text=transcript,
            language=language,
            specialty=definition.specialty,
            authorization=auth_header,
        )
        content = _content_for_template(
            definition=definition,
            template_id=template_id,
            schema_version=schema_version,
            transcript=transcript,
            title=title,
            encounter_date=body.encounter_date,
            extracted_fields=extracted_fields,
        )

        code = await code_sequence.next_code(conn, tenant_id=claims.tid)
        try:
            report_id, version_id = await repo.create_report_with_v1(
                conn,
                tenant_id=claims.tid,
                code=code,
                primary_author_id=claims.sub,
                co_author_ids=[],
                patient_id=body.patient_id,
                patient_name_redacted=patient_name_redacted,
                template_id=template_id,
                template_schema_version=schema_version,
                source_session_id=None,
                content=content,
                source_asr_job_id=body.asr_job_id,
            )
        except asyncpg.UniqueViolationError:
            # Concurrent double-assign lost the race on the partial index.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "already_assigned", "detail": "assigned concurrently"},
            ) from None

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_CREATED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={
            "code": code,
            "version_id": str(version_id),
            "source_asr_job_id": str(body.asr_job_id),
            "template_selection": selection,
            "template_id": str(template_id),
        },
        severity=Severity.INFO,
    )

    return FromTranscriptResponse(
        id=report_id,
        code=code,
        version_id=version_id,
        version_number=1,
        status="draft",
        patient_id=body.patient_id,
        template_id=template_id,
        template_name=template_name,
        template_selection=selection,  # type: ignore[arg-type]
        template_score=score,
    )


@router.get("/by-source-job", response_model=list[SourceJobLink])
async def reports_by_source_job(
    claims: Annotated[Claims, Depends(requires("report.read", "report"))],
    ids: Annotated[str, Query(description="Comma-separated asr job UUIDs (≤200).")],
) -> list[SourceJobLink]:
    try:
        job_ids = [UUID(part) for part in ids.split(",") if part.strip()]
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ids must be UUIDs"
        ) from None
    if not job_ids or len(job_ids) > 200:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="between 1 and 200 ids")
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        rows = await repo.fetch_reports_by_source_jobs(conn, asr_job_ids=job_ids)
    return [
        SourceJobLink(
            asr_job_id=row["source_asr_job_id"],
            report_id=row["id"],
            code=row["code"],
            status=str(row["status"]),
            patient_id=row["patient_id"],
        )
        for row in rows
    ]


def _parse_definition(row: asyncpg.Record) -> TemplateDefinition:
    import json

    raw = row["schema_jsonb"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    try:
        return TemplateDefinition.model_validate(raw)
    except Exception:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "template_invalid", "detail": "template schema failed to parse"},
        ) from None
