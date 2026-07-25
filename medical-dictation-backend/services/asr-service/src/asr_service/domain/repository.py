"""Repository functions for asr-service.

Lives in ``domain/`` so router/adapter layers cannot accidentally bypass
it. Every query is tenant-scoped via :func:`db.tenant_connection`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from asr_models import JobStatus, TranscriptionJobView


async def insert_audio_row(
    conn: asyncpg.Connection,
    *,
    audio_id: UUID,
    tenant_id: UUID,
    uploader_sub: UUID,
    mime_type: str,
    size_bytes: int,
    duration_ms: int,
    sha256: bytes,
    envelope_metadata: dict[str, Any],
    storage_uri: str,
    encounter_id: UUID | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO audio_files
            (id, tenant_id, uploader_sub, mime_type, size_bytes,
             duration_ms, sha256, envelope_metadata, storage_uri, status,
             encounter_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, 'stored', $10)
        """,
        audio_id,
        tenant_id,
        uploader_sub,
        mime_type,
        size_bytes,
        duration_ms,
        sha256,
        json.dumps(envelope_metadata),
        storage_uri,
        encounter_id,
    )


async def fetch_encounter_status(
    conn: asyncpg.Connection, *, encounter_id: UUID
) -> str | None:
    """Status of the encounter, or None when nonexistent / cross-tenant —
    RLS scopes the query, so a foreign tenant's encounter is invisible
    (no existence oracle)."""
    return await conn.fetchval(
        "SELECT status FROM encounters WHERE id = $1", encounter_id
    )


async def insert_job_row(
    conn: asyncpg.Connection,
    *,
    job_id: UUID,
    tenant_id: UUID,
    audio_id: UUID,
    requester_sub: UUID,
    prompt_id: UUID,
    language: str,
    model: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO transcription_jobs
            (id, tenant_id, audio_id, requester_sub, prompt_id, language, model)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        job_id,
        tenant_id,
        audio_id,
        requester_sub,
        prompt_id,
        language,
        model,
    )


async def get_job(conn: asyncpg.Connection, *, job_id: UUID) -> TranscriptionJobView | None:
    row = await conn.fetchrow(
        "SELECT * FROM transcription_jobs WHERE id = $1",
        job_id,
    )
    if row is None:
        return None
    return _row_to_view(row)


async def list_jobs(
    conn: asyncpg.Connection,
    *,
    limit: int,
    status: JobStatus | None = None,
    since: datetime | None = None,
) -> list[TranscriptionJobView]:
    where_parts: list[str] = []
    args: list[Any] = []
    if status is not None:
        where_parts.append(f"j.status = ${len(args) + 1}")
        args.append(str(status))
    if since is not None:
        where_parts.append(f"j.queued_at >= ${len(args) + 1}")
        args.append(since)
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    args.append(limit)
    # S14 — carry the patient through so a dictation list can name whose
    # recording each row is. LEFT JOINs throughout: a job with no
    # encounter (a plain upload) and a job whose patient RLS hides must
    # both still appear. `where_sql` predicates are on transcription_jobs
    # columns only, so the alias keeps them unambiguous.
    rows = await conn.fetch(
        f"""
        SELECT j.*,
               e.patient_id      AS patient_id,
               p.name_uk         AS patient_name_uk,
               p.name_en         AS patient_name_en
        FROM transcription_jobs j
        LEFT JOIN audio_files a ON a.id = j.audio_id
        LEFT JOIN encounters  e ON e.id = a.encounter_id
        LEFT JOIN patients    p ON p.id = e.patient_id
        {where_sql}
        ORDER BY j.queued_at DESC
        LIMIT ${len(args)}
        """,
        *args,
    )
    return [_row_to_view(r) for r in rows]


async def request_cancel(conn: asyncpg.Connection, *, job_id: UUID) -> str | None:
    """Mark the job for cancellation; return the new status or ``None``
    if it cannot be cancelled (already terminal).
    """
    row = await conn.fetchrow(
        "SELECT status FROM transcription_jobs WHERE id = $1 FOR UPDATE",
        job_id,
    )
    if row is None:
        return None
    current = str(row["status"])
    if current == "queued":
        await conn.execute(
            """
            UPDATE transcription_jobs
            SET status='cancelled', finished_at=now(), cancel_requested=true
            WHERE id = $1
            """,
            job_id,
        )
        return "cancelled"
    if current == "running":
        await conn.execute(
            "UPDATE transcription_jobs SET cancel_requested=true WHERE id = $1",
            job_id,
        )
        return "cancel_requested"
    return None


async def count_active_jobs(conn: asyncpg.Connection, *, tenant_id: UUID) -> int:
    """Return the number of queued + running jobs for the tenant.

    Used by the rate-limit check (per-tenant concurrent cap).
    """
    row = await conn.fetchrow(
        """
        SELECT COUNT(*) AS n
        FROM transcription_jobs
        WHERE status IN ('queued','running')
        """,
    )
    return int(row["n"]) if row is not None else 0


@dataclass(slots=True)
class PromptRow:
    """One ``medical_prompts`` catalogue entry (metadata only — no prompt_text)."""

    id: UUID
    language: str
    specialty: str
    is_default: bool


async def list_prompts(
    conn: asyncpg.Connection, *, language: str | None = None, specialty: str | None = None
) -> list[PromptRow]:
    """List the global ``medical_prompts`` catalogue (ADR-0007, no RLS).

    The picker must surface the same UUIDs ``submit_job`` stores, so this
    reads ``medical_prompts`` directly — not report-service section prompts.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if language is not None:
        params.append(language)
        clauses.append(f"language = ${len(params)}")
    if specialty is not None:
        params.append(specialty)
        clauses.append(f"specialty = ${len(params)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await conn.fetch(
        f"""
        SELECT id, language, specialty, is_default
        FROM medical_prompts
        {where}
        ORDER BY specialty, is_default DESC
        """,
        *params,
    )
    return [
        PromptRow(
            id=r["id"],
            language=r["language"],
            specialty=r["specialty"],
            is_default=bool(r["is_default"]),
        )
        for r in rows
    ]


def _row_to_view(row: asyncpg.Record) -> TranscriptionJobView:
    return TranscriptionJobView(
        id=row["id"],
        tenant_id=row["tenant_id"],
        audio_id=row["audio_id"],
        requester_sub=row["requester_sub"],
        prompt_id=row["prompt_id"],
        language=row["language"],
        model=row["model"],
        status=JobStatus(row["status"]),
        error_kind=row["error_kind"],
        error_detail=row["error_detail"],
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        attempts=int(row["attempts"]),
        # Present only on the list projection, which joins them in.
        patient_id=row.get("patient_id"),
        patient_name_uk=row.get("patient_name_uk"),
        patient_name_en=row.get("patient_name_en"),
    )
