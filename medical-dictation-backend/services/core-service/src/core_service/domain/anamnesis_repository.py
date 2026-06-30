"""Anamnesis repository — RLS-scoped upsert over ``patient_anamnesis``."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg

_COLUMNS = "patient_id, tenant_id, record, updated_by, updated_at"


async def get_anamnesis(
    conn: asyncpg.Connection, *, patient_id: UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM patient_anamnesis WHERE patient_id = $1",
        patient_id,
    )


async def upsert_anamnesis(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    patient_id: UUID,
    updated_by: UUID,
    record: dict[str, Any],
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO patient_anamnesis (patient_id, tenant_id, record, updated_by)
        VALUES ($1, $2, $3::jsonb, $4)
        ON CONFLICT (patient_id) DO UPDATE
        SET record = EXCLUDED.record,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        RETURNING {_COLUMNS}
        """,
        patient_id,
        tenant_id,
        json.dumps(record),
        updated_by,
    )
