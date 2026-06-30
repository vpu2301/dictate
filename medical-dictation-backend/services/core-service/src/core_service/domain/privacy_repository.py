"""Privacy-requests repository — RLS-scoped SQL over
``patient_privacy_requests`` (DSAR + erasure log)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

_COLUMNS = """
    id, tenant_id, patient_id, kind, reason, status,
    requested_by, requested_at, scheduled_for
"""


async def create_request(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    patient_id: UUID,
    requested_by: UUID,
    kind: str,
    reason: str,
    status: str,
    scheduled_for: datetime | None,
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO patient_privacy_requests
            (tenant_id, patient_id, kind, reason, status, requested_by, scheduled_for)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING {_COLUMNS}
        """,
        tenant_id,
        patient_id,
        kind,
        reason,
        status,
        requested_by,
        scheduled_for,
    )


async def list_for_patient(
    conn: asyncpg.Connection, *, patient_id: UUID
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM patient_privacy_requests
            WHERE patient_id = $1
            ORDER BY requested_at DESC
            """,
            patient_id,
        )
    )
