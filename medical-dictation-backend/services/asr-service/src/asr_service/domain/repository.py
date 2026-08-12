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


async def prompt_exists(conn: asyncpg.Connection, *, prompt_id: UUID) -> bool:
    """Whether ``prompt_id`` names a row in the global prompt catalogue.

    ``transcription_jobs.prompt_id`` carries a NOT NULL FK to
    ``medical_prompts``, so an unknown id used to surface as an asyncpg
    ForeignKeyViolationError from the INSERT — a 500 on the SPA for what is
    a caller mistake (a stale prompt id cached from a previous release).
    Checked before any ciphertext is written so the reject costs nothing.

    ``medical_prompts`` is a global catalogue with no RLS (ADR-0007), the
    same table ``list_prompts`` serves the picker from, so existence here
    is not a cross-tenant oracle.
    """
    return bool(
        await conn.fetchval("SELECT 1 FROM medical_prompts WHERE id = $1", prompt_id)
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


async def fail_job(
    conn: asyncpg.Connection,
    *,
    job_id: UUID,
    error_kind: str,
    error_detail: str,
    only_if_status: tuple[str, ...] = ("queued", "running"),
) -> bool:
    """Move a job to ``failed``; return whether this call is what moved it.

    ``only_if_status`` is the interlock. The reaper and the enqueue path
    both write terminal failures from outside the worker that owns the
    job, and a job that came back to life between the read and the write
    must keep its own outcome — a transcript already stored must never be
    overwritten by a late "the worker looked dead".
    """
    row = await conn.fetchrow(
        """
        UPDATE transcription_jobs
        SET status='failed',
            error_kind=$2,
            error_detail=$3,
            finished_at=now()
        WHERE id = $1 AND status = ANY($4::text[])
        RETURNING id
        """,
        job_id,
        error_kind,
        error_detail[:1024],
        list(only_if_status),
    )
    return row is not None


@dataclass(slots=True)
class StaleJobRow:
    """A job that has outlived the process or the queue that owned it."""

    id: UUID
    status: str
    requester_sub: UUID
    started_at: datetime | None


async def list_stale_jobs(
    conn: asyncpg.Connection,
    *,
    running_grace_seconds: float,
    queued_grace_seconds: float,
    limit: int,
) -> list[StaleJobRow]:
    """Jobs stranded mid-flight, oldest first.

    Two shapes, one query:

    - ``running`` past ``running_grace_seconds`` — the worker that claimed
      it died between marking it running and writing an outcome. Nothing
      else in the system ever revisits that row.
    - ``queued`` past ``queued_grace_seconds`` — the enqueue landed but the
      message did not survive (a flushed Redis, a stream trimmed under
      load), so no worker will ever claim it.

    Both windows are wall-clock only; the reaper applies its own
    liveness interlock before it collects anything.
    """
    rows = await conn.fetch(
        """
        SELECT id, status, requester_sub, started_at
        FROM transcription_jobs
        WHERE (status = 'running' AND started_at < now()
                   - make_interval(secs => $1::double precision))
           OR (status = 'queued'  AND queued_at  < now()
                   - make_interval(secs => $2::double precision))
        ORDER BY queued_at
        LIMIT $3
        """,
        float(running_grace_seconds),
        float(queued_grace_seconds),
        limit,
    )
    return [
        StaleJobRow(
            id=r["id"],
            status=str(r["status"]),
            requester_sub=r["requester_sub"],
            started_at=r["started_at"],
        )
        for r in rows
    ]


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
        cancel_requested=bool(row.get("cancel_requested") or False),
        # Present only on the list projection, which joins them in.
        patient_id=row.get("patient_id"),
        patient_name_uk=row.get("patient_name_uk"),
        patient_name_en=row.get("patient_name_en"),
    )
