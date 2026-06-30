"""Patients repository — RLS-scoped SQL over ``patients``.

Every function receives a connection already bound to ``app.tenant_id`` via
:func:`db.tenant_connection`; Postgres RLS enforces the tenant scope, so these
queries never spell out ``WHERE tenant_id = …`` for isolation (only for the
INSERT value).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

import asyncpg

_COLUMNS = """
    id, tenant_id, name_uk, name_en, dob, sex, mrn,
    summary_uk, summary_en, tags, status, last_visit_at,
    created_by, created_at, updated_at
"""


async def create_patient(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    created_by: UUID,
    name_uk: str,
    name_en: str,
    dob: date | None,
    sex: str,
    mrn: str,
    summary_uk: str,
    summary_en: str,
    tags: list[str],
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO patients
            (tenant_id, name_uk, name_en, dob, sex, mrn,
             summary_uk, summary_en, tags, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING {_COLUMNS}
        """,
        tenant_id,
        name_uk,
        name_en,
        dob,
        sex,
        mrn,
        summary_uk,
        summary_en,
        tags,
        created_by,
    )


async def get_patient(
    conn: asyncpg.Connection, *, patient_id: UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM patients WHERE id = $1",
        patient_id,
    )


async def list_patients(
    conn: asyncpg.Connection,
    *,
    query: str | None,
    limit: int,
    cursor: tuple[datetime, UUID] | None,
) -> list[asyncpg.Record]:
    """Roster page ordered by recency (``COALESCE(last_visit_at, created_at)``
    DESC, id DESC). Keyset-paginated; fetches ``limit + 1`` so the caller can
    detect a next page."""
    where: list[str] = []
    args: list[Any] = []
    if query:
        args.append(f"%{query}%")
        where.append(
            f"(name_uk ILIKE ${len(args)} OR name_en ILIKE ${len(args)} "
            f"OR mrn ILIKE ${len(args)})"
        )
    if cursor is not None:
        args.append(cursor[0])
        args.append(cursor[1])
        where.append(
            f"(COALESCE(last_visit_at, created_at), id) < (${len(args) - 1}, ${len(args)})"
        )
    args.append(limit + 1)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return list(
        await conn.fetch(
            f"""
            SELECT {_COLUMNS}
            FROM patients
            {where_sql}
            ORDER BY COALESCE(last_visit_at, created_at) DESC, id DESC
            LIMIT ${len(args)}
            """,
            *args,
        )
    )


async def update_patient(
    conn: asyncpg.Connection,
    *,
    patient_id: UUID,
    fields: dict[str, Any],
) -> asyncpg.Record | None:
    """Patch a whitelist of columns. ``fields`` keys must be column names;
    callers (the router) own the whitelist, never the request body."""
    if not fields:
        return await get_patient(conn, patient_id=patient_id)
    sets: list[str] = []
    args: list[Any] = []
    for col, val in fields.items():
        args.append(val)
        sets.append(f"{col} = ${len(args)}")
    args.append(patient_id)
    return await conn.fetchrow(
        f"""
        UPDATE patients
        SET {", ".join(sets)}, updated_at = now()
        WHERE id = ${len(args)}
        RETURNING {_COLUMNS}
        """,
        *args,
    )


async def bump_last_visit(
    conn: asyncpg.Connection, *, patient_id: UUID, when: datetime
) -> None:
    """Advance ``last_visit_at`` if ``when`` is newer (never moves it back)."""
    await conn.execute(
        """
        UPDATE patients
        SET last_visit_at = GREATEST(COALESCE(last_visit_at, $2), $2),
            updated_at = now()
        WHERE id = $1
        """,
        patient_id,
        when,
    )
