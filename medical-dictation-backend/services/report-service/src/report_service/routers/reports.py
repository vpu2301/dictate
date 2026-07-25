"""POST /reports + GET /reports/{id} — sprint-08 day-1/day-6."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims
from db import tenant_connection
from report_models import ReadPurpose, ReportContent

from .. import audit_kinds
from ..deps import get_state, requires
from ..domain import code_sequence
from ..domain import reports_repository as repo
from ..domain.pii_redactor import name_to_initials
from ._content_guard import ensure_valid_field_metadata
from ._phi_access_guard import ReportReadAccess, report_read_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


# ── Request / response shapes ───────────────────────────────────────


class CreateReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: ReportContent
    patient_id: UUID
    co_author_ids: list[UUID] = Field(default_factory=list)
    source_session_id: UUID | None = None


class ReportCreatedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    code: str
    version_id: UUID
    version_number: int
    status: str


class LocalizedText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uk: str
    en: str


class SectionLabel(BaseModel):
    """Human-readable, localized title for one report section.

    Templates are per-language (a template row is either ``uk`` or ``en``),
    so the section ``name`` is a single string in the template's own
    language. We mirror it into BOTH ``uk`` and ``en`` here — the same
    behaviour the frontend's ``toStudioTemplate`` uses — so the SPA/PDF can
    render section titles without re-fetching the template, and historical
    reports carry their labels even if the template later changes.
    """

    model_config = ConfigDict(extra="forbid")

    section_key: str
    name: LocalizedText


class ReportEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    code: str
    status: str
    current_version_id: UUID
    current_version_number: int
    primary_author_id: UUID
    co_author_ids: list[UUID]
    patient_id: UUID | None
    patient_name_redacted: str | None
    title: str
    icd10_codes: list[str]
    encounter_date: str | None
    created_at: str
    updated_at: str
    finalized_at: str | None
    signed_at: str | None
    cancelled_at: str | None
    content: ReportContent | None = None
    section_labels: list[SectionLabel] | None = None


# ── Helpers ─────────────────────────────────────────────────────────


def _envelope(
    row: repo.ReportRow,
    *,
    content: ReportContent | None = None,
    section_labels: list[SectionLabel] | None = None,
) -> ReportEnvelope:
    return ReportEnvelope(
        id=row.id,
        code=row.code,
        status=row.status.value,
        current_version_id=row.current_version_id,
        current_version_number=row.current_version_number,
        primary_author_id=row.primary_author_id,
        co_author_ids=row.co_author_ids,
        patient_id=row.patient_id,
        patient_name_redacted=row.patient_name_redacted,
        title=row.title,
        icd10_codes=row.icd10_codes,
        encounter_date=row.encounter_date.isoformat() if row.encounter_date else None,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
        finalized_at=row.finalized_at.isoformat() if row.finalized_at else None,
        signed_at=row.signed_at.isoformat() if row.signed_at else None,
        cancelled_at=row.cancelled_at.isoformat() if row.cancelled_at else None,
        content=content,
        section_labels=section_labels,
    )


async def _resolve_section_labels(
    conn: object, *, content: ReportContent
) -> list[SectionLabel] | None:
    """Build localized section labels from the report's template.

    Resolves the template by ``content.template_id`` (reusing the domain
    ``get_template`` repository helper within the caller's RLS-scoped
    connection) and emits one :class:`SectionLabel` per template section,
    ordered by the section ``order``. Returns ``None`` — never raises — if
    the template was deleted or cannot be parsed, so a missing template
    degrades gracefully instead of 500-ing the read.

    Note: only the current template row is persisted per ``template_id``
    (cosmetic edits update in place), so we resolve against it; section
    names are cosmetic and never participate in ``body_hash``.
    """
    import json

    from template_models import TemplateDefinition

    from ..domain.repository import get_template

    try:
        tmpl_row = await get_template(conn, template_id=content.template_id)  # type: ignore[arg-type]
        if tmpl_row is None:
            return None
        raw = tmpl_row["schema_jsonb"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        definition = TemplateDefinition.model_validate(raw)
    except Exception:
        logger.warning(
            "could not resolve template %s for section labels",
            content.template_id,
            exc_info=True,
        )
        return None

    return [
        # Templates are per-language; mirror the single name into both
        # locales (matches the frontend's toStudioTemplate behaviour).
        SectionLabel(section_key=section.id, name=LocalizedText(uk=section.name, en=section.name))
        for section in sorted(definition.sections, key=lambda s: s.order)
    ]


# ── Routes ──────────────────────────────────────────────────────────


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ReportCreatedResponse,
    responses={
        422: {
            "description": "`patient_not_found`, or sprint-13 field-metadata "
            "validation: `field_metadata_invalid` / `choice_value_unknown` "
            "(section-addressed problems in `problems[]`)."
        }
    },
)
async def create_report(
    body: CreateReportRequest,
    claims: Annotated[Claims, Depends(requires("report.write", "report"))],
) -> ReportCreatedResponse:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        # Defence-in-depth: a report MUST reference a patient in the caller's
        # own tenant. Resolve under RLS so a cross-tenant / missing patient
        # both surface as 422 patient_not_found (never 404 — don't leak
        # cross-tenant existence).
        patient = await repo.fetch_patient_label(conn, patient_id=body.patient_id)
        if patient is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "type": "https://errors.medical-dictation/patient-not-found",
                    "title": "Patient not found",
                    "detail": "patient_not_found",
                    "code": "patient_not_found",
                },
            )
        # Prefer the Ukrainian name; fall back to English. Store initials only.
        patient_name_redacted = name_to_initials(patient.name_uk or patient.name_en)

        # Sprint-13: typed field metadata must be valid at every write.
        await ensure_valid_field_metadata(conn, content=body.content)

        code = await code_sequence.next_code(conn, tenant_id=claims.tid)
        report_id, version_id = await repo.create_report_with_v1(
            conn,
            tenant_id=claims.tid,
            code=code,
            primary_author_id=claims.sub,
            co_author_ids=body.co_author_ids,
            patient_id=body.patient_id,
            patient_name_redacted=patient_name_redacted,
            template_id=body.content.template_id,
            template_schema_version=body.content.template_schema_version,
            source_session_id=body.source_session_id,
            content=body.content,
        )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_CREATED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={"code": code, "version_id": str(version_id)},
        severity=Severity.INFO,
    )

    return ReportCreatedResponse(
        id=report_id,
        code=code,
        version_id=version_id,
        version_number=1,
        status="draft",
    )


@router.get("/{report_id}", response_model=ReportEnvelope)
async def get_report(
    report_id: UUID,
    access: Annotated[ReportReadAccess, Depends(report_read_access)],
    purpose: Annotated[
        ReadPurpose | None,
        Query(description="Required for non-author reads."),
    ] = None,
    include_content: bool = Query(default=True),
) -> ReportEnvelope:
    # Two standings reach this handler (S14): ordinary `report.read`, or a
    # live break-glass grant on THIS report. The guard has already
    # resolved which and counted the use; from here the only difference
    # is what the audit trail says.
    claims = access.claims
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        row = await repo.fetch_report(conn, report_id=report_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="report not found")

        # Read-purpose enforcement: required if requester is not author/co-author.
        is_author = claims.sub == row.primary_author_id or claims.sub in row.co_author_ids
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

        content_obj: ReportContent | None = None
        section_labels: list[SectionLabel] | None = None
        if include_content:
            v = await repo.fetch_version(conn, version_id=row.current_version_id)
            content_obj = v.content if v else None
            if content_obj is not None:
                section_labels = await _resolve_section_labels(conn, content=content_obj)

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.REPORT_VIEWED_FULL,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="report",
        target_id=report_id,
        payload={
            "purpose": purpose.value if purpose else "author",
            "is_author": is_author,
            "break_glass": access.is_break_glass,
        },
        severity=Severity.SEC if access.is_break_glass else Severity.INFO,
    )
    if access.is_break_glass:
        # A second, distinctly-kinded event so "every break-glass read"
        # is one query over the chain rather than a filter over every
        # report view ever recorded.
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
                "surface": "report_envelope",
            },
            severity=Severity.SEC,
        )

    return _envelope(row, content=content_obj, section_labels=section_labels)
