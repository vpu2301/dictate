"""Clinical-notes repository — RLS-scoped SQL over ``clinical_notes``."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

# S14 — the notes list carries the patient's real name for the roles that
# read it (clinician / nurse; an admin cannot reach this surface at all).
# It is a LEFT JOIN, not an inner one: a note whose patient row is gone
# must still appear in the feed. Losing the name is a display problem;
# losing the note is a clinical record problem.
#
# The join adds no tenant predicate — `patients` carries its own RLS
# policy, so a cross-tenant patient is invisible here for the same reason
# it is invisible to a direct SELECT.
_NOTE_COLUMNS_QUALIFIED = """
    n.id, n.tenant_id, n.patient_id, n.encounter_id, n.structure, n.title,
    n.sections, n.status, n.author_id, n.source_session_id,
    n.created_at, n.updated_at, n.signed_at,
    p.name_uk AS patient_name_uk,
    p.name_en AS patient_name_en
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
    created = await conn.fetchrow(
        """
        INSERT INTO clinical_notes
            (tenant_id, patient_id, encounter_id, structure, title,
             sections, author_id, source_session_id)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
        RETURNING id
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
    # Re-read so a write returns the SAME shape as a read, patient embed
    # included. A router forced to branch on "did this row come from a
    # write?" would eventually forget to, and the note would render with
    # a bare UUID where every other surface shows a name.
    note = await get_note(conn, note_id=created["id"])
    assert note is not None  # just inserted, same transaction
    return note


async def get_note(
    conn: asyncpg.Connection, *, note_id: UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"""
        SELECT {_NOTE_COLUMNS_QUALIFIED}
        FROM clinical_notes n
        LEFT JOIN patients p ON p.id = n.patient_id
        WHERE n.id = $1
        """,
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
        where.append(f"n.patient_id = ${len(args)}")
    if status is not None:
        args.append(status)
        where.append(f"n.status = ${len(args)}")
    args.append(limit)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return list(
        await conn.fetch(
            f"""
            SELECT {_NOTE_COLUMNS_QUALIFIED}
            FROM clinical_notes n
            LEFT JOIN patients p ON p.id = n.patient_id
            {where_sql}
            ORDER BY n.updated_at DESC, n.id DESC
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
    updated = await conn.fetchrow(
        f"""
        UPDATE clinical_notes
        SET {", ".join(sets)}, updated_at = now()
        WHERE id = ${len(args)}
        RETURNING id
        """,
        *args,
    )
    return None if updated is None else await get_note(conn, note_id=updated["id"])


async def sign_note(
    conn: asyncpg.Connection, *, note_id: UUID, when: datetime
) -> asyncpg.Record | None:
    signed = await conn.fetchrow(
        """
        UPDATE clinical_notes
        SET status = 'signed', signed_at = $2, updated_at = now()
        WHERE id = $1 AND status = 'draft'
        RETURNING id
        """,
        note_id,
        when,
    )
    return None if signed is None else await get_note(conn, note_id=signed["id"])
