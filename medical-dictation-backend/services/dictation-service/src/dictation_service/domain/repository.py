"""Repository helpers for dictation_sessions + audio_files.

Every query is tenant-scoped via :func:`db.tenant_connection`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from ..session.state import SessionState


async def insert_session(
    conn: asyncpg.Connection,
    *,
    session_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
    language: str,
    prompt_id: UUID,
    target_kind: str,
    encounter_id: UUID | None,
    template_id: UUID | None,
    worker_id: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO dictation_sessions
            (id, tenant_id, user_id, language, prompt_id, target_kind,
             encounter_id, template_id, worker_id, status, started_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'active',now())
        """,
        session_id,
        tenant_id,
        user_id,
        language,
        prompt_id,
        target_kind,
        encounter_id,
        template_id,
        worker_id,
    )


async def get_session(
    conn: asyncpg.Connection, *, session_id: UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT * FROM dictation_sessions WHERE id = $1",
        session_id,
    )


async def list_active_sessions_for_user(
    conn: asyncpg.Connection, *, user_id: UUID, limit: int = 50
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            """
            SELECT id, status, language, target_kind, started_at, last_active_at
            FROM dictation_sessions
            WHERE user_id = $1 AND status IN ('active','paused','reconnecting','creating')
            ORDER BY last_active_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
    )


async def list_sessions(
    conn: asyncpg.Connection,
    *,
    status: SessionState | None = None,
    since: datetime | None = None,
    limit: int = 50,
) -> list[asyncpg.Record]:
    parts: list[str] = []
    args: list[Any] = []
    if status is not None:
        parts.append(f"status = ${len(args) + 1}")
        args.append(str(status))
    if since is not None:
        parts.append(f"last_active_at >= ${len(args) + 1}")
        args.append(since)
    where_sql = ("WHERE " + " AND ".join(parts)) if parts else ""
    args.append(limit)
    return list(
        await conn.fetch(
            f"""
            SELECT id, status, language, target_kind,
                   started_at, last_active_at, finalized_at,
                   total_audio_ms, network_drop_count
            FROM dictation_sessions
            {where_sql}
            ORDER BY last_active_at DESC
            LIMIT ${len(args)}
            """,
            *args,
        )
    )


async def update_status(
    conn: asyncpg.Connection,
    *,
    session_id: UUID,
    new_status: SessionState,
    error_kind: str | None = None,
    error_detail: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE dictation_sessions
        SET status = $2,
            error_kind = COALESCE($3, error_kind),
            error_detail = COALESCE($4, error_detail),
            last_active_at = now()
        WHERE id = $1
        """,
        session_id,
        str(new_status),
        error_kind,
        error_detail,
    )


async def touch_last_active(conn: asyncpg.Connection, *, session_id: UUID) -> None:
    await conn.execute(
        "UPDATE dictation_sessions SET last_active_at = now() WHERE id = $1",
        session_id,
    )


async def write_finalized(
    conn: asyncpg.Connection,
    *,
    session_id: UUID,
    audio_file_id: UUID | None,
    transcript_jsonb: list[dict[str, Any]],
    total_audio_ms: int,
    total_speech_ms: int,
    avg_partial_latency_ms: int | None,
    avg_final_latency_ms: int | None,
    rtf: float | None,
    network_drop_count: int,
    truncated: bool,
) -> None:
    await conn.execute(
        """
        UPDATE dictation_sessions
        SET status='finalized',
            finalized_at = now(),
            audio_file_id = $2,
            transcript_jsonb = $3::jsonb,
            total_audio_ms = $4,
            total_speech_ms = $5,
            avg_partial_latency_ms = $6,
            avg_final_latency_ms = $7,
            rtf = $8,
            network_drop_count = $9,
            truncated = $10
        WHERE id = $1
        """,
        session_id,
        audio_file_id,
        json.dumps(transcript_jsonb),
        total_audio_ms,
        total_speech_ms,
        avg_partial_latency_ms,
        avg_final_latency_ms,
        rtf,
        network_drop_count,
        truncated,
    )


async def count_active_for_tenant(
    conn: asyncpg.Connection, *, tenant_id: UUID
) -> int:
    row = await conn.fetchrow(
        """
        SELECT COUNT(*) AS n
        FROM dictation_sessions
        WHERE status IN ('active','paused','reconnecting','creating')
        """,
    )
    return int(row["n"]) if row is not None else 0
