"""Dense retrieval over pgvector HNSW (mandatory engine — down ⇒ 503)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from db import tenant_connection

from evidence_models import (
    ConnectorKind,
    EvidencePassage,
    EvidenceTier,
    LicenseClass,
    SourceAuthority,
)
from evidence_retrieval.domain.plan import Filters, compile_sql

_BASE_SELECT = """
SELECT c.id AS chunk_id, c.document_version_id, c.section_path, c.char_start,
       c.char_end, c.text,
       d.id AS document_id, d.evidence_tier, d.source_authority, d.published_at,
       d.license_class, d.retracted,
       1 - (c.embedding <=> $2::vector) AS dense_score
FROM chunks c
JOIN document_versions dv ON dv.id = c.document_version_id
JOIN documents d ON d.id = dv.document_id
WHERE c.tenant_id = $1 AND c.embedding IS NOT NULL AND NOT d.retracted
"""

_LATEST_ONLY = """
  AND dv.version = (SELECT max(v2.version) FROM document_versions v2
                    WHERE v2.document_id = d.id)
"""


def _row_to_passage(
    row: asyncpg.Record, *, kind: ConnectorKind, connector_id: str
) -> EvidencePassage:
    return EvidencePassage(
        id=str(row["chunk_id"]),
        connector_id=connector_id,
        text=row["text"],
        section_path=row["section_path"],
        document_version_id=row["document_version_id"],
        evidence_tier=EvidenceTier(row["evidence_tier"]),
        source_authority=SourceAuthority(row["source_authority"]),
        published_at=row["published_at"],
        score=float(row["dense_score"]),
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        source_kind=kind,
        char_start=row["char_start"],
        char_end=row["char_end"],
        license_class=LicenseClass(row["license_class"]),
        retracted=row["retracted"],
    )


async def dense_search(
    pool: asyncpg.Pool,
    *,
    tenant_id: UUID,
    partition_tenant: UUID,
    query_vector: list[float],
    k: int,
    filters: Filters,
    snapshot_members: list[UUID] | None,
    kind: ConnectorKind,
    connector_id: str,
) -> list[EvidencePassage]:
    """tenant_id scopes RLS (caller); partition_tenant selects the corpus
    partition (GLOBAL or the caller's own)."""
    vector_literal = "[" + ",".join(f"{x:.7f}" for x in query_vector) + "]"
    sql = _BASE_SELECT
    params: list[Any] = [partition_tenant, vector_literal]
    n = 3
    if snapshot_members is not None:
        sql += f"  AND dv.id = ANY(${n}::uuid[])\n"
        params.append(snapshot_members)
        n += 1
    elif not filters.include_superseded:
        sql += _LATEST_ONLY
    fragment, extra = compile_sql(filters, first_param=n)
    sql += fragment
    params.extend(extra)
    n += len(extra)
    sql += f" ORDER BY c.embedding <=> $2::vector, c.id LIMIT ${n}"
    params.append(k)
    async with tenant_connection(pool, tenant_id) as conn:
        rows = await conn.fetch(sql, *params)
    return [_row_to_passage(r, kind=kind, connector_id=connector_id) for r in rows]


async def snapshot_member_versions(
    pool: asyncpg.Pool, *, tenant_id: UUID, snapshot_id: UUID
) -> list[UUID] | None:
    async with tenant_connection(pool, tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT member_versions FROM corpus_snapshots WHERE id = $1", snapshot_id
        )
    return list(row["member_versions"]) if row else None
