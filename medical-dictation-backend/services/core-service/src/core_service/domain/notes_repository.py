"""Clinical-notes repository — RLS-scoped SQL over ``clinical_notes``."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

_COLUMNS = """
    id, tenant_id, patient_id, encounter_id, structure, title,
    sections, status, author_id, source_session_id,
    created_at, updated_at, signed_at
"""


async def create_note(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    patient_id: UUID,
    encounter_id: UUID | None,
    author_id: UUID,
    structure: str,
    title: str,
    sections: list[dict[str, Any]],
    source_session_id: UUID | None,
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO clinical_notes
            (tenant_id, patient_id, encounter_id, structure, title,
             sections, author_id, source_session_id)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
        RETURNING {_COLUMNS}
        """,
        tenant_id,
        patient_id,
        encounter_id,
        structure,
        title,
        json.dumps(sections),
        author_id,
        source_session_id,
    )


async def get_note(
    conn: asyncpg.Connection, *, note_id: UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM clinical_notes WHERE id = $1",
        note_id,
    )


async def list_notes(
    conn: asyncpg.Connection,
    *,
    patient_id: UUID | None,
    status: str | None,
    limit: int,
) -> list[asyncpg.Record]:
    where: list[str] = []
    args: list[Any] = []
    if patient_id is not None:
        args.append(patient_id)
        where.append(f"patient_id = ${len(args)}")
    if status is not None:
        args.append(status)
        where.append(f"status = ${len(args)}")
    args.append(limit)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return list(
        await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM clinical_notes
            {where_sql}
            ORDER BY updated_at DESC, id DESC
            LIMIT ${len(args)}
            """,
            *args,
        )
    )


async def update_note(
    conn: asyncpg.Connection,
    *,
    note_id: UUID,
    fields: dict[str, Any],
) -> asyncpg.Record | None:
    if not fields:
        return await get_note(conn, note_id=note_id)
    sets: list[str] = []
    args: list[Any] = []
    for col, val in fields.items():
        args.append(json.dumps(val) if col == "sections" else val)
        cast = "::jsonb" if col == "sections" else ""
        sets.append(f"{col} = ${len(args)}{cast}")
    args.append(note_id)
    return await conn.fetchrow(
        f"""
        UPDATE clinical_notes
        SET {", ".join(sets)}, updated_at = now()
        WHERE id = ${len(args)}
        RETURNING {_COLUMNS}
        """,
        *args,
    )


async def sign_note(
    conn: asyncpg.Connection, *, note_id: UUID, when: datetime
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"""
        UPDATE clinical_notes
        SET status = 'signed', signed_at = $2, updated_at = now()
        WHERE id = $1 AND status = 'draft'
        RETURNING {_COLUMNS}
        """,
        note_id,
        when,
    )
