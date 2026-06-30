"""Patient-consents repository — RLS-scoped SQL over ``patient_consents``."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

_COLUMNS = """
    id, tenant_id, patient_id, encounter_id, type, method, version,
    status, granted_at, withdrawn_at, created_by
"""


async def create_consent(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    patient_id: UUID,
    encounter_id: UUID | None,
    created_by: UUID,
    type_: str,
    method: str,
    version: str,
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO patient_consents
            (tenant_id, patient_id, encounter_id, type, method, version, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING {_COLUMNS}
        """,
        tenant_id,
        patient_id,
        encounter_id,
        type_,
        method,
        version,
        created_by,
    )


async def list_for_patient(
    conn: asyncpg.Connection, *, patient_id: UUID
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM patient_consents
            WHERE patient_id = $1
            ORDER BY granted_at DESC
            """,
            patient_id,
        )
    )


async def withdraw_consent(
    conn: asyncpg.Connection,
    *,
    consent_id: UUID,
    patient_id: UUID,
    when: datetime,
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"""
        UPDATE patient_consents
        SET status = 'withdrawn', withdrawn_at = $3
        WHERE id = $1 AND patient_id = $2 AND status = 'granted'
        RETURNING {_COLUMNS}
        """,
        consent_id,
        patient_id,
        when,
    )
