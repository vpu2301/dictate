"""Encounters repository — RLS-scoped SQL over ``encounters``."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

from . import encounter_state

_COLUMNS = """
    id, tenant_id, patient_id, kind, reason, occurred_at,
    status, created_by, created_at, started_at, ended_at, updated_at
"""


def _initial_timestamps(status: str, occurred_at: datetime) -> tuple[datetime | None, datetime | None]:
    """(started_at, ended_at) for a freshly inserted row.

    ``encounters_ended_has_ts`` (0058) is a biconditional, so a row created
    directly in a terminal status — the retro-log flow, where the clinician
    records a visit that already happened — must carry ``ended_at`` at
    INSERT or the write is rejected.
    """
    started = occurred_at if status != encounter_state.SCHEDULED else None
    ended = occurred_at if encounter_state.is_terminal(status) else None
    if status == encounter_state.CANCELLED:
        started = None
    return started, ended


async def create_encounter(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    patient_id: UUID,
    created_by: UUID,
    kind: str,
    reason: str,
    occurred_at: datetime,
    status: str,
) -> asyncpg.Record:
    started_at, ended_at = _initial_timestamps(status, occurred_at)
    return await conn.fetchrow(
        f"""
        INSERT INTO encounters
            (tenant_id, patient_id, kind, reason, occurred_at, status,
             created_by, started_at, ended_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING {_COLUMNS}
        """,
        tenant_id,
        patient_id,
        kind,
        reason,
        occurred_at,
        status,
        created_by,
        started_at,
        ended_at,
    )


async def list_for_patient(
    conn: asyncpg.Connection, *, patient_id: UUID, limit: int = 200
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM encounters
            WHERE patient_id = $1
            ORDER BY occurred_at DESC
            LIMIT $2
            """,
            patient_id,
            limit,
        )
    )


async def get_encounter(
    conn: asyncpg.Connection, *, encounter_id: UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM encounters WHERE id = $1",
        encounter_id,
    )


#: The queue surfaces (today's schedule, open visits) are worklists: a row
#: without a patient name is unusable, and one round-trip per row is an N+1
#: the roster already avoids. Both tables are tenant-local and RLS-scoped,
#: so the join costs nothing extra.
_QUEUE_COLUMNS = """
    e.id, e.tenant_id, e.patient_id, e.kind, e.reason, e.occurred_at,
    e.status, e.created_by, e.created_at, e.started_at, e.ended_at,
    e.updated_at,
    p.name_uk AS patient_name_uk, p.name_en AS patient_name_en,
    p.mrn AS patient_mrn, p.dob AS patient_dob, p.sex AS patient_sex
"""


async def list_schedule(
    conn: asyncpg.Connection, *, day_start: datetime, day_end: datetime
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"""
            SELECT {_QUEUE_COLUMNS}
            FROM encounters e
            JOIN patients p ON p.id = e.patient_id
            WHERE e.status = 'scheduled'
              AND e.occurred_at >= $1 AND e.occurred_at < $2
            ORDER BY e.occurred_at ASC
            """,
            day_start,
            day_end,
        )
    )


async def list_open(
    conn: asyncpg.Connection,
    *,
    created_by: UUID | None = None,
    limit: int = 100,
) -> list[asyncpg.Record]:
    """Visits still holding a slot in the pipeline (in_progress | paused).

    ``created_by`` narrows to the caller's own visits; a tenant_admin
    chasing everything left open passes None. Served by
    ``encounters_open_idx`` (0058).
    """
    return list(
        await conn.fetch(
            f"""
            SELECT {_QUEUE_COLUMNS}
            FROM encounters e
            JOIN patients p ON p.id = e.patient_id
            WHERE e.status IN ('in_progress', 'paused')
              AND ($1::uuid IS NULL OR e.created_by = $1)
            ORDER BY e.updated_at DESC
            LIMIT $2
            """,
            created_by,
            limit,
        )
    )


async def update_lifecycle(
    conn: asyncpg.Connection,
    *,
    encounter_id: UUID,
    expected_status: str,
    new_status: str,
    now: datetime,
) -> asyncpg.Record | None:
    """Compare-and-set the status, stamping the lifecycle timestamps.

    The ``status = $2`` predicate makes this a CAS against the row the
    caller validated, so two clinicians racing to end the same visit
    produce one winner and one 409 rather than two audit events.

    ``started_at`` is stamped only on the first transition into
    ``in_progress`` (COALESCE), so a pause/resume cycle does not reset the
    visit's clock.
    """
    return await conn.fetchrow(
        f"""
        UPDATE encounters
           SET status     = $3,
               started_at = CASE WHEN $3 = 'in_progress'
                                 THEN COALESCE(started_at, $4)
                                 ELSE started_at END,
               ended_at   = CASE WHEN $3 IN ('completed', 'cancelled')
                                 THEN $4
                                 ELSE NULL END,
               updated_at = $4
         WHERE id = $1 AND status = $2
        RETURNING {_COLUMNS}
        """,
        encounter_id,
        expected_status,
        new_status,
        now,
    )


async def count_live_sessions(
    conn: asyncpg.Connection, *, encounter_id: UUID, stale_after_seconds: int
) -> int:
    """How many dictation sessions on this encounter are genuinely live.

    Read-only peek across the service boundary, same as
    ``timeline_repository`` — core-service never writes ``dictation_sessions``.

    "Genuinely live" excludes rows stranded by a dead worker: those keep a
    non-terminal status forever (dictation-service's reaper clears them
    asynchronously) and must not be able to wedge a visit open. Only a
    session that has been heard from inside the staleness window blocks
    ending the visit.
    """
    value = await conn.fetchval(
        """
        SELECT count(*) FROM dictation_sessions
        WHERE encounter_id = $1
          AND status IN ('creating', 'active', 'paused')
          AND last_active_at > now() - make_interval(secs => $2::double precision)
        """,
        encounter_id,
        float(stale_after_seconds),
    )
    return int(value or 0)
