"""Patient-consents repository — RLS-scoped SQL over ``patient_consents``."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

_COLUMNS = """
    id, tenant_id, patient_id, encounter_id, type, method, version,
    status, granted_at, withdrawn_at, created_by,
    signed_envelope_id, canonical_hash
"""


async def create_consent(
    conn: asyncpg.Connection,
    *,
    consent_id: UUID,
    tenant_id: UUID,
    patient_id: UUID,
    encounter_id: UUID | None,
    created_by: UUID,
    type_: str,
    method: str,
    version: str,
    granted_at: datetime,
    canonical_hash: bytes | None = None,
) -> asyncpg.Record:
    """Insert a consent. ``consent_id`` AND ``granted_at`` are
    caller-generated (not DB defaults): the canonical document of a digital
    consent embeds both, and signing recomputes it from the row — the
    persisted values must be byte-identical to what was canonicalized."""
    return await conn.fetchrow(
        f"""
        INSERT INTO patient_consents
            (id, tenant_id, patient_id, encounter_id, type, method, version,
             created_by, granted_at, canonical_hash)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING {_COLUMNS}
        """,
        consent_id,
        tenant_id,
        patient_id,
        encounter_id,
        type_,
        method,
        version,
        created_by,
        granted_at,
        canonical_hash,
    )


async def get_consent(
    conn: asyncpg.Connection, *, consent_id: UUID, patient_id: UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM patient_consents WHERE id = $1 AND patient_id = $2",
        consent_id,
        patient_id,
    )


async def fetch_user_display_name(
    conn: asyncpg.Connection, *, sub: UUID
) -> str | None:
    """Display name of an in-tenant user (RLS-scoped read of ``users``)."""
    return await conn.fetchval("SELECT display_name FROM users WHERE sub = $1", sub)


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
