"""Reports + report_versions repository (sprint-08).

All queries run on a tenant-scoped connection (``app.tenant_id`` set
by ``db.tenant_connection``). RLS does the rest.

The repository is deliberately a thin SQL wrapper — domain rules
(state machine, finalize validation, optimistic check) live in
sibling modules. This makes the property test in
``tests/property/test_amendment_chain.py`` straightforward.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

import asyncpg

from report_models import (
    ReportAmendmentType,
    ReportContent,
    ReportStatus,
    canonical_content_bytes,
    rendered_text_from_content,
)

logger = logging.getLogger(__name__)


def body_hash_for(content: ReportContent) -> str:
    """sha256 of the canonical body — used for autosave idempotency."""
    return hashlib.sha256(canonical_content_bytes(content)).hexdigest()


def _icd10_codes_as_strings(content: ReportContent) -> list[str]:
    seen: dict[str, None] = {}
    for c in content.icd10_codes:
        seen[c.code] = None
    for s in content.sections:
        for c in s.icd10:
            seen[c.code] = None
    return list(seen)


def _parse_iso_date(value: str | None) -> date | None:
    """ReportContent stores encounter_date as an ISO string; asyncpg binds
    ``$n::date`` params as real dates, so a bare str raises DataError
    ("'str' object has no attribute 'toordinal'"). Normalize here so every
    write path is safe. An unparseable value becomes NULL rather than a 500."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@dataclass(slots=True)
class ReportRow:
    id: UUID
    tenant_id: UUID
    code: str
    status: ReportStatus
    current_version_id: UUID
    current_version_number: int
    primary_author_id: UUID
    co_author_ids: list[UUID]
    title: str
    icd10_codes: list[str]
    encounter_date: datetime | None
    created_at: datetime
    updated_at: datetime
    finalized_at: datetime | None
    signed_at: datetime | None
    cancelled_at: datetime | None
    source_session_id: UUID | None = None
    patient_id: UUID | None = None
    patient_name_redacted: str | None = None


@dataclass(slots=True)
class VersionRow:
    id: UUID
    report_id: UUID
    version_number: int
    parent_version_id: UUID | None
    created_by: UUID
    created_at: datetime
    content: ReportContent
    rendered_text: str
    body_hash: str | None
    is_amendment: bool
    amendment_type: ReportAmendmentType | None
    amendment_reason: str | None
    signed_at: datetime | None
    signed_by: UUID | None


# ── Read ────────────────────────────────────────────────────────────


async def fetch_report(conn: asyncpg.Connection, *, report_id: UUID) -> ReportRow | None:
    row = await conn.fetchrow(
        """
        SELECT r.id, r.tenant_id, r.code, r.status,
               r.current_version_id, v.version_number AS current_version_number,
               r.primary_author_id, r.co_author_ids,
               r.title, r.icd10_codes, r.encounter_date,
               r.created_at, r.updated_at, r.finalized_at,
               r.signed_at, r.cancelled_at, r.source_session_id,
               r.patient_id, r.patient_name_redacted
        FROM reports r
        LEFT JOIN report_versions v ON v.id = r.current_version_id
        WHERE r.id = $1
        """,
        report_id,
    )
    if row is None:
        return None
    return ReportRow(
        id=row["id"],
        tenant_id=row["tenant_id"],
        code=row["code"],
        status=ReportStatus(row["status"]),
        current_version_id=row["current_version_id"],
        current_version_number=int(row["current_version_number"] or 0),
        primary_author_id=row["primary_author_id"],
        co_author_ids=list(row["co_author_ids"] or []),
        title=row["title"],
        icd10_codes=list(row["icd10_codes"] or []),
        encounter_date=row["encounter_date"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finalized_at=row["finalized_at"],
        signed_at=row["signed_at"],
        cancelled_at=row["cancelled_at"],
        source_session_id=row["source_session_id"],
        patient_id=row["patient_id"],
        patient_name_redacted=row["patient_name_redacted"],
    )


async def set_source_session_id_if_absent(
    conn: asyncpg.Connection, *, report_id: UUID, session_id: UUID
) -> None:
    """Backfill ``source_session_id`` only when it is still NULL.

    Used by finalize to link a dictation session to its report. A report
    that already carries a source session is left untouched (no-op).
    """
    await conn.execute(
        """
        UPDATE reports
        SET source_session_id = $2, updated_at = now()
        WHERE id = $1 AND source_session_id IS NULL
        """,
        report_id,
        session_id,
    )


async def fetch_version(conn: asyncpg.Connection, *, version_id: UUID) -> VersionRow | None:
    row = await conn.fetchrow(
        """
        SELECT id, report_id, version_number, parent_version_id,
               created_by, created_at,
               content_jsonb, rendered_text, metadata,
               is_amendment, amendment_type, amendment_reason,
               signed_at, signed_by
        FROM report_versions
        WHERE id = $1
        """,
        version_id,
    )
    if row is None:
        return None
    raw = row["content_jsonb"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    body_hash = metadata.get("body_hash") if isinstance(metadata, dict) else None
    return VersionRow(
        id=row["id"],
        report_id=row["report_id"],
        version_number=int(row["version_number"]),
        parent_version_id=row["parent_version_id"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        content=ReportContent.model_validate(raw),
        rendered_text=row["rendered_text"],
        body_hash=body_hash,
        is_amendment=bool(row["is_amendment"]),
        amendment_type=(
            ReportAmendmentType(row["amendment_type"]) if row["amendment_type"] else None
        ),
        amendment_reason=row["amendment_reason"],
        signed_at=row["signed_at"],
        signed_by=row["signed_by"],
    )


async def fetch_version_by_number(
    conn: asyncpg.Connection, *, report_id: UUID, version_number: int
) -> VersionRow | None:
    row = await conn.fetchval(
        "SELECT id FROM report_versions WHERE report_id = $1 AND version_number = $2",
        report_id,
        version_number,
    )
    if row is None:
        return None
    return await fetch_version(conn, version_id=row)


@dataclass(slots=True)
class VersionSummaryRow:
    """Lightweight version-list row — never decodes ``content_jsonb``."""

    id: UUID
    version_number: int
    parent_version_id: UUID | None
    created_by: UUID
    created_at: datetime
    is_amendment: bool
    amendment_type: ReportAmendmentType | None
    amendment_reason: str | None
    signed_at: datetime | None
    signed_by: UUID | None


async def list_version_summaries(
    conn: asyncpg.Connection, *, report_id: UUID
) -> list[VersionSummaryRow]:
    """All versions of a report as metadata-only summaries, oldest first."""
    rows = await conn.fetch(
        """
        SELECT id, version_number, parent_version_id, created_by, created_at,
               is_amendment, amendment_type, amendment_reason, signed_at, signed_by
        FROM report_versions
        WHERE report_id = $1
        ORDER BY version_number
        """,
        report_id,
    )
    return [
        VersionSummaryRow(
            id=r["id"],
            version_number=int(r["version_number"]),
            parent_version_id=r["parent_version_id"],
            created_by=r["created_by"],
            created_at=r["created_at"],
            is_amendment=bool(r["is_amendment"]),
            amendment_type=(
                ReportAmendmentType(r["amendment_type"]) if r["amendment_type"] else None
            ),
            amendment_reason=r["amendment_reason"],
            signed_at=r["signed_at"],
            signed_by=r["signed_by"],
        )
        for r in rows
    ]


# ── Patient resolution ──────────────────────────────────────────────


@dataclass(slots=True)
class PatientLabel:
    """Minimal patient projection used at report-create time.

    Resolved on the caller's RLS-scoped connection, so a row is returned
    only when the patient lives in the caller's tenant. Cross-tenant /
    missing patients both surface as ``None``.
    """

    id: UUID
    name_uk: str
    name_en: str


async def fetch_patient_label(conn: asyncpg.Connection, *, patient_id: UUID) -> PatientLabel | None:
    """Resolve a patient for report creation under the current RLS scope.

    Returns ``None`` for a missing patient, a patient in another tenant
    (RLS hides it), or a soft-deleted (``status`` = ``deceased``/``inactive``
    is still valid — only tenant scoping filters here). The router turns a
    ``None`` into a 422 ``patient_not_found`` (never a 404) so cross-tenant
    existence is not leaked.
    """
    row = await conn.fetchrow(
        "SELECT id, name_uk, name_en FROM patients WHERE id = $1",
        patient_id,
    )
    if row is None:
        return None
    return PatientLabel(
        id=row["id"],
        name_uk=row["name_uk"],
        name_en=row["name_en"],
    )


async def fetch_patient_labels(
    conn: asyncpg.Connection, *, patient_ids: Sequence[UUID]
) -> dict[UUID, PatientLabel]:
    """Batch form of :func:`fetch_patient_label`, for list surfaces.

    S14: nurses and clinicians see the patient's real name in the report
    list rather than the frozen initials in
    ``reports.patient_name_redacted``. Resolving live also fixes a
    long-standing staleness bug — the initials are written once at report
    creation, so a patient renamed (or erased to ``ERASED``) afterwards
    kept their old initials on every historical report.

    One query for the whole page, keyed by id. Missing ids simply do not
    appear in the mapping: RLS hides other tenants' patients, and the
    caller must render a hit whose patient it cannot resolve.
    """
    if not patient_ids:
        return {}
    rows = await conn.fetch(
        "SELECT id, name_uk, name_en FROM patients WHERE id = ANY($1::uuid[])",
        list(dict.fromkeys(patient_ids)),
    )
    return {
        row["id"]: PatientLabel(
            id=row["id"], name_uk=row["name_uk"], name_en=row["name_en"]
        )
        for row in rows
    }


# ── Create ──────────────────────────────────────────────────────────


async def create_report_with_v1(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    code: str,
    primary_author_id: UUID,
    co_author_ids: list[UUID],
    patient_id: UUID,
    patient_name_redacted: str,
    template_id: UUID,
    template_schema_version: int,
    source_session_id: UUID | None,
    content: ReportContent,
    source_asr_job_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    """Two-step insert (ADR-0020):

    1. INSERT report with NULL current_version_id.
    2. INSERT v1 in report_versions.
    3. UPDATE report.current_version_id.

    The deferrable FK constraint is satisfied at COMMIT.
    Caller MUST be inside a single transaction (tenant_connection
    already opens one).
    """
    rendered = rendered_text_from_content(content)
    body_hash = body_hash_for(content)
    icd10_codes = _icd10_codes_as_strings(content)
    # ReportContent.encounter_date is an ISO string; asyncpg binds the
    # $11::date parameter as a real date, so hand it a date object (a bare
    # str raises "'str' object has no attribute 'toordinal'").
    encounter_date = _parse_iso_date(content.encounter_date)

    report_id: UUID = await conn.fetchval(
        """
        INSERT INTO reports (
            tenant_id, code, status, primary_author_id, co_author_ids,
            patient_id, patient_name_redacted, template_id, template_schema_version,
            title, icd10_codes, encounter_date, source_session_id, source_asr_job_id
        )
        VALUES ($1, $2, 'draft', $3, $4, $5, $6, $7, $8, $9, $10, $11::date, $12, $13)
        RETURNING id
        """,
        tenant_id,
        code,
        primary_author_id,
        co_author_ids,
        patient_id,
        patient_name_redacted,
        template_id,
        template_schema_version,
        content.title,
        icd10_codes,
        encounter_date,
        source_session_id,
        source_asr_job_id,
    )

    version_id: UUID = await conn.fetchval(
        """
        INSERT INTO report_versions (
            report_id, version_number, parent_version_id, created_by,
            content_jsonb, rendered_text, diff_jsonb, metadata
        )
        VALUES ($1, 1, NULL, $2, $3::jsonb, $4, '{}'::jsonb, $5::jsonb)
        RETURNING id
        """,
        report_id,
        primary_author_id,
        json.dumps(content.model_dump(mode="json")),
        rendered,
        json.dumps({"body_hash": body_hash}),
    )

    await conn.execute(
        "UPDATE reports SET current_version_id = $2, updated_at = now() WHERE id = $1",
        report_id,
        version_id,
    )
    return report_id, version_id


async def fetch_reports_by_source_jobs(
    conn: asyncpg.Connection, *, asr_job_ids: list[UUID]
) -> list[asyncpg.Record]:
    """Reports created from the given transcription jobs (RLS-scoped).

    Powers the jobs-list "already assigned" badge — bulk, one round trip.
    """
    return await conn.fetch(
        """
        SELECT source_asr_job_id, id, code, status, patient_id
        FROM reports
        WHERE source_asr_job_id = ANY($1::uuid[])
        """,
        asr_job_ids,
    )


# ── Append version (autosave / amendment) ───────────────────────────


async def append_version(
    conn: asyncpg.Connection,
    *,
    report_id: UUID,
    expected_version: int,
    new_content: ReportContent,
    created_by: UUID,
    diff_jsonb: dict[str, Any],
    is_amendment: bool = False,
    amendment_type: ReportAmendmentType | None = None,
    amendment_reason: str | None = None,
    parent_version_id_override: UUID | None = None,
    body_hash_override: str | None = None,
) -> tuple[UUID, int]:
    """Append a new version row to ``report_id``.

    Concurrency:
    - Caller obtains a row lock via ``SELECT ... FOR UPDATE`` on the
      reports row before calling this. We re-check ``version_number``
      here (defence-in-depth) so two callers cannot both think they
      hold the lock.

    Returns (new_version_id, new_version_number).
    """
    head = await conn.fetchrow(
        """
        SELECT v.id, v.version_number
        FROM reports r
        JOIN report_versions v ON v.id = r.current_version_id
        WHERE r.id = $1
        """,
        report_id,
    )
    if head is None:
        raise RuntimeError("report has no current_version; corrupt state")
    if int(head["version_number"]) != expected_version:
        from .conflicts import OptimisticLockMismatchError

        raise OptimisticLockMismatchError(
            current_version=int(head["version_number"]),
            expected_version=expected_version,
        )

    rendered = rendered_text_from_content(new_content)
    body_hash = body_hash_override or body_hash_for(new_content)
    new_version_number = expected_version + 1
    parent_version_id = parent_version_id_override or head["id"]
    icd10_codes = _icd10_codes_as_strings(new_content)

    new_version_id: UUID = await conn.fetchval(
        """
        INSERT INTO report_versions (
            report_id, version_number, parent_version_id, created_by,
            content_jsonb, rendered_text, diff_jsonb, metadata,
            is_amendment, amendment_type, amendment_reason
        )
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8::jsonb,
                $9, $10, $11)
        RETURNING id
        """,
        report_id,
        new_version_number,
        parent_version_id,
        created_by,
        json.dumps(new_content.model_dump(mode="json")),
        rendered,
        json.dumps(diff_jsonb),
        json.dumps({"body_hash": body_hash}),
        is_amendment,
        amendment_type.value if amendment_type else None,
        amendment_reason,
    )
    await conn.execute(
        """
        UPDATE reports
        SET current_version_id = $2,
            title              = $3,
            icd10_codes        = $4,
            encounter_date     = $5::date,
            updated_at         = now()
        WHERE id = $1
        """,
        report_id,
        new_version_id,
        new_content.title,
        icd10_codes,
        _parse_iso_date(new_content.encounter_date),
    )
    return new_version_id, new_version_number


# ── Helpers used by routers ─────────────────────────────────────────


async def lock_report_for_update(conn: asyncpg.Connection, *, report_id: UUID) -> ReportRow | None:
    """Acquire a row lock on the report (used by autosave / amend).

    Combined with the optimistic ``expected_version`` check, this
    serialises concurrent writers; one wins, the other gets 409.
    """
    await conn.fetchrow(
        "SELECT id FROM reports WHERE id = $1 FOR UPDATE",
        report_id,
    )
    return await fetch_report(conn, report_id=report_id)


async def find_existing_version_by_body_hash(
    conn: asyncpg.Connection,
    *,
    report_id: UUID,
    body_hash: str,
) -> VersionRow | None:
    """Idempotency lookup: did we already record this exact body for
    this report? Used to make autosave PUTs idempotent on retry."""
    row_id = await conn.fetchval(
        """
        SELECT id FROM report_versions
        WHERE report_id = $1 AND metadata->>'body_hash' = $2
        ORDER BY version_number DESC LIMIT 1
        """,
        report_id,
        body_hash,
    )
    if row_id is None:
        return None
    return await fetch_version(conn, version_id=row_id)


async def list_amendment_chain(conn: asyncpg.Connection, *, report_id: UUID) -> list[VersionRow]:
    """Returns all versions for ``report_id`` ordered by version_number ASC.

    Used by the chain reconciler + the diff endpoint when resolving
    'from'/'to' by number.
    """
    rows = await conn.fetch(
        "SELECT id FROM report_versions WHERE report_id = $1 ORDER BY version_number",
        report_id,
    )
    out: list[VersionRow] = []
    for r in rows:
        v = await fetch_version(conn, version_id=r["id"])
        if v is not None:
            out.append(v)
    return out
