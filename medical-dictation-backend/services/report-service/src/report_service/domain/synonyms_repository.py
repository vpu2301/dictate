"""medical_synonyms repository (sprint 15, ADR-0038).

Free functions on an RLS-scoped connection (house idiom). Lexemes are
computed IN the database via ``to_tsvector('simple', …)`` so write-time
normalization can never drift from query-time normalization. RLS
enforces scope: app_role writes reach only tenant rows of the caller's
tenant; system rows are read-only to the app (no PERMISSIVE write
policy grants them).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import asyncpg


@dataclass(slots=True, frozen=True)
class SynonymGroup:
    group_id: UUID
    source: str
    language: str
    terms: list[str]


async def list_groups(
    conn: asyncpg.Connection, *, include_system: bool = True
) -> list[SynonymGroup]:
    rows = await conn.fetch(
        "SELECT group_id, source, language, array_agg(term ORDER BY term) AS terms "
        "FROM medical_synonyms "
        + ("" if include_system else "WHERE source = 'tenant' ")
        + "GROUP BY group_id, source, language "
        "ORDER BY source, min(term)"
    )
    return [
        SynonymGroup(
            group_id=r["group_id"],
            source=r["source"],
            language=r["language"],
            terms=list(r["terms"]),
        )
        for r in rows
    ]


async def create_group(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    terms: list[str],
    language: str,
    created_by: UUID,
) -> UUID:
    group_id = uuid4()
    await conn.executemany(
        "INSERT INTO medical_synonyms "
        "  (tenant_id, group_id, term, lexemes, language, source, created_by) "
        "VALUES ($1, $2, $3, tsvector_to_array(to_tsvector('simple', $3)), "
        "        $4, 'tenant', $5)",
        [(tenant_id, group_id, term, language, created_by) for term in terms],
    )
    return group_id


async def replace_group(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    group_id: UUID,
    terms: list[str],
    language: str,
    created_by: UUID,
) -> bool:
    """Replace a tenant group's terms atomically. False when the group has
    no tenant rows (system groups delete zero rows — RLS makes them
    unreachable for writes)."""
    async with conn.transaction():
        deleted = await conn.fetch(
            "DELETE FROM medical_synonyms WHERE group_id = $1 AND source = 'tenant' "
            "RETURNING id",
            group_id,
        )
        if not deleted:
            return False
        await conn.executemany(
            "INSERT INTO medical_synonyms "
            "  (tenant_id, group_id, term, lexemes, language, source, created_by) "
            "VALUES ($1, $2, $3, tsvector_to_array(to_tsvector('simple', $3)), "
            "        $4, 'tenant', $5)",
            [(tenant_id, group_id, term, language, created_by) for term in terms],
        )
    return True


async def delete_group(conn: asyncpg.Connection, *, group_id: UUID) -> bool:
    rows = await conn.fetch(
        "DELETE FROM medical_synonyms WHERE group_id = $1 AND source = 'tenant' "
        "RETURNING id",
        group_id,
    )
    return bool(rows)
