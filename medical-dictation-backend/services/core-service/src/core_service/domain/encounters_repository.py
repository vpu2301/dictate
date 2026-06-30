"""Encounters repository — RLS-scoped SQL over ``encounters``."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

_COLUMNS = """
    id, tenant_id, patient_id, kind, reason, occurred_at,
    status, created_by, created_at
"""


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
    return await conn.fetchrow(
        f"""
        INSERT INTO encounters
            (tenant_id, patient_id, kind, reason, occurred_at, status, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING {_COLUMNS}
        """,
        tenant_id,
        patient_id,
        kind,
        reason,
        occurred_at,
        status,
        created_by,
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


async def list_schedule(
    conn: asyncpg.Connection, *, day_start: datetime, day_end: datetime
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM encounters
            WHERE status = 'scheduled'
              AND occurred_at >= $1 AND occurred_at < $2
            ORDER BY occurred_at ASC
            """,
            day_start,
            day_end,
        )
    )
