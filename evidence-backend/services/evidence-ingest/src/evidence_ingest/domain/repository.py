"""DB access for the ingest pipeline. Every function takes an RLS-scoped
connection (from tenant_connection) positionally; everything else keyword-only
(platform repository convention)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID

import asyncpg


def _as_date(value: str | None) -> date | None:
    """Lenient ISO/eu-dotted date parse; unparseable dates degrade to None
    rather than killing the job (published_at is metadata, not integrity)."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        pass
    try:
        day, month, year = value.strip()[:10].split(".")
        return date(int(year), int(month), int(day))
    except (ValueError, AttributeError):
        return None


# ── ingest_jobs ─────────────────────────────────────────────────────────


async def create_job(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    source_uri: str,
    kind: str,
    idempotence_key: str,
    metadata_overrides: dict[str, str],
) -> UUID | None:
    """Insert a job; None when the idempotence key already exists (no-op re-ingest)."""
    return await conn.fetchval(
        """
        INSERT INTO ingest_jobs (tenant_id, source_uri, kind, idempotence_key, metadata_overrides)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (tenant_id, idempotence_key) DO NOTHING
        RETURNING id
        """,
        tenant_id,
        source_uri,
        kind,
        idempotence_key,
        json.dumps(metadata_overrides),
    )


async def get_job(conn: asyncpg.Connection, *, job_id: UUID) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM ingest_jobs WHERE id = $1", job_id)


async def set_job_state(
    conn: asyncpg.Connection,
    *,
    job_id: UUID,
    state: str,
    stage_seconds: float | None = None,
    stage: str | None = None,
) -> None:
    if stage is not None and stage_seconds is not None:
        await conn.execute(
            """
            UPDATE ingest_jobs
            SET state = $2,
                timings = timings || jsonb_build_object($3::text, $4::numeric),
                updated_at = now()
            WHERE id = $1
            """,
            job_id,
            state,
            stage,
            round(stage_seconds, 3),
        )
    else:
        await conn.execute(
            "UPDATE ingest_jobs SET state = $2, updated_at = now() WHERE id = $1",
            job_id,
            state,
        )


async def record_failure(
    conn: asyncpg.Connection,
    *,
    job_id: UUID,
    tenant_id: UUID,
    stage: str,
    error: str,
    dead: bool,
    payload_ref: str | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE ingest_jobs
        SET attempts = attempts + 1, last_error = $2,
            state = CASE WHEN $3 THEN 'dead' ELSE state END,
            updated_at = now()
        WHERE id = $1
        """,
        job_id,
        f"{stage}: {error}"[:2000],
        dead,
    )
    if dead:
        await conn.execute(
            """
            INSERT INTO ingest_errors (tenant_id, job_id, stage, error, payload_ref)
            VALUES ($1, $2, $3, $4, $5)
            """,
            tenant_id,
            job_id,
            stage,
            error[:4000],
            payload_ref,
        )


async def link_document(
    conn: asyncpg.Connection,
    *,
    job_id: UUID,
    document_id: UUID,
    document_version_id: UUID | None,
) -> None:
    await conn.execute(
        "UPDATE ingest_jobs SET document_id = $2, document_version_id = $3, updated_at = now()"
        " WHERE id = $1",
        job_id,
        document_id,
        document_version_id,
    )


# ── documents / versions / chunks ───────────────────────────────────────


async def upsert_document(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    canonical_id: str,
    title: str,
    source_authority: str,
    evidence_tier: str,
    jurisdiction: str | None,
    specialty: list[str],
    published_at: str | None,
    license_class: str,
) -> UUID:
    """Insert or newest-wins update of the document header row (spec D4)."""
    return await conn.fetchval(
        """
        INSERT INTO documents (tenant_id, canonical_id, title, source_authority,
                               evidence_tier, jurisdiction, specialty, published_at, license_class)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (tenant_id, canonical_id) DO UPDATE
            SET title = EXCLUDED.title,
                source_authority = EXCLUDED.source_authority,
                evidence_tier = EXCLUDED.evidence_tier,
                jurisdiction = EXCLUDED.jurisdiction,
                specialty = EXCLUDED.specialty,
                published_at = COALESCE(EXCLUDED.published_at, documents.published_at),
                license_class = EXCLUDED.license_class
        RETURNING id
        """,
        tenant_id,
        canonical_id,
        title,
        source_authority,
        evidence_tier,
        jurisdiction,
        specialty,
        _as_date(published_at),
        license_class,
    )


async def latest_version(conn: asyncpg.Connection, *, document_id: UUID) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT id, version, checksum FROM document_versions"
        " WHERE document_id = $1 ORDER BY version DESC LIMIT 1",
        document_id,
    )


async def insert_version(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    document_id: UUID,
    version: int,
    content_ref: str,
    parsed_ref: str | None,
    checksum: str,
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO document_versions (tenant_id, document_id, version, content_ref,
                                       parsed_ref, checksum)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
        """,
        tenant_id,
        document_id,
        version,
        content_ref,
        parsed_ref,
        checksum,
    )


async def insert_chunks(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    document_version_id: UUID,
    chunks: list[dict[str, Any]],
) -> int:
    await conn.executemany(
        """
        INSERT INTO chunks (tenant_id, document_version_id, section_path,
                            char_start, char_end, text)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        [
            (
                tenant_id,
                document_version_id,
                c["section_path"],
                c["char_start"],
                c["char_end"],
                c["text"],
            )
            for c in chunks
        ],
    )
    return len(chunks)


async def pending_embedding_batch(
    conn: asyncpg.Connection, *, document_version_id: UUID, limit: int
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            "SELECT id, text FROM chunks WHERE document_version_id = $1 AND embedding IS NULL"
            " ORDER BY char_start LIMIT $2",
            document_version_id,
            limit,
        )
    )


async def store_embeddings(
    conn: asyncpg.Connection, *, ids: list[UUID], vectors: list[list[float]]
) -> None:
    await conn.executemany(
        "UPDATE chunks SET embedding = $2::vector WHERE id = $1",
        [
            (chunk_id, "[" + ",".join(f"{x:.7f}" for x in vec) + "]")
            for chunk_id, vec in zip(ids, vectors, strict=True)
        ],
    )


async def chunks_for_version(
    conn: asyncpg.Connection, *, document_version_id: UUID
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            "SELECT id, section_path, char_start, char_end, text FROM chunks"
            " WHERE document_version_id = $1 ORDER BY char_start",
            document_version_id,
        )
    )


# ── quarantine ──────────────────────────────────────────────────────────


async def create_quarantine(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    job_id: UUID,
    document_ref: str,
    reason: str,
    patterns: list[dict[str, str]],
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO quarantine (tenant_id, job_id, document_ref, reason, patterns)
        VALUES ($1, $2, $3, $4, $5) RETURNING id
        """,
        tenant_id,
        job_id,
        document_ref,
        reason,
        json.dumps(patterns),
    )


async def decide_quarantine(
    conn: asyncpg.Connection,
    *,
    quarantine_id: UUID,
    reviewed_by: UUID,
    decision: str,
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        UPDATE quarantine
        SET reviewed_by = $2, decision = $3, reviewed_at = now()
        WHERE id = $1 AND decision IS NULL
        RETURNING job_id, tenant_id
        """,
        quarantine_id,
        reviewed_by,
        decision,
    )


# ── snapshots / retractions / stats ─────────────────────────────────────


async def pending_job_count(conn: asyncpg.Connection, *, tenant_id: UUID) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM ingest_jobs WHERE tenant_id = $1 AND state NOT IN ('done', 'dead')",
        tenant_id,
    )


async def snapshot_members(
    conn: asyncpg.Connection, *, tenant_id: UUID, allowed_licenses: frozenset[str]
) -> list[asyncpg.Record]:
    """Latest version of every non-retracted, license-admitted document."""
    return list(
        await conn.fetch(
            """
            SELECT DISTINCT ON (d.id) d.id AS document_id, d.license_class, d.retracted,
                   dv.id AS version_id
            FROM documents d
            JOIN document_versions dv ON dv.document_id = d.id
            WHERE d.tenant_id = $1 AND NOT d.retracted
              AND d.license_class = ANY($2::text[])
            ORDER BY d.id, dv.version DESC
            """,
            tenant_id,
            list(allowed_licenses),
        )
    )


async def excluded_license_count(
    conn: asyncpg.Connection, *, tenant_id: UUID, allowed_licenses: frozenset[str]
) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM documents WHERE tenant_id = $1 AND NOT retracted"
        " AND NOT (license_class = ANY($2::text[]))",
        tenant_id,
        list(allowed_licenses),
    )


async def create_snapshot(
    conn: asyncpg.Connection, *, tenant_id: UUID, label: str, member_versions: list[UUID]
) -> UUID:
    return await conn.fetchval(
        "INSERT INTO corpus_snapshots (tenant_id, label, member_versions)"
        " VALUES ($1, $2, $3) RETURNING id",
        tenant_id,
        label,
        member_versions,
    )


async def mark_retracted(
    conn: asyncpg.Connection, *, tenant_id: UUID, canonical_ids: list[str]
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            """
            UPDATE documents SET retracted = true
            WHERE tenant_id = $1 AND canonical_id = ANY($2::text[]) AND NOT retracted
            RETURNING id, canonical_id
            """,
            tenant_id,
            canonical_ids,
        )
    )


async def corpus_stats(conn: asyncpg.Connection, *, tenant_id: UUID) -> dict[str, Any]:
    jobs = await conn.fetch(
        "SELECT state, count(*) AS n FROM ingest_jobs WHERE tenant_id = $1 GROUP BY state",
        tenant_id,
    )
    docs = await conn.fetch(
        "SELECT source_authority, evidence_tier, count(*) AS n FROM documents"
        " WHERE tenant_id = $1 GROUP BY source_authority, evidence_tier",
        tenant_id,
    )
    chunk_count = await conn.fetchval("SELECT count(*) FROM chunks WHERE tenant_id = $1", tenant_id)
    embedded = await conn.fetchval(
        "SELECT count(*) FROM chunks WHERE tenant_id = $1 AND embedding IS NOT NULL", tenant_id
    )
    quarantine_open = await conn.fetchval(
        "SELECT count(*) FROM quarantine WHERE tenant_id = $1 AND decision IS NULL", tenant_id
    )
    return {
        "jobs_by_state": {r["state"]: r["n"] for r in jobs},
        "documents": [dict(r) for r in docs],
        "chunks_total": chunk_count,
        "chunks_embedded": embedded,
        "quarantine_open": quarantine_open,
    }
