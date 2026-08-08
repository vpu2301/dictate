"""DB access for the web agent. Every function takes an RLS-scoped connection
(from `tenant_connection`) positionally; everything else keyword-only
(platform repository convention)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

# ── web_domains ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DomainRow:
    id: UUID
    tenant_id: UUID
    domain: str
    trust_tier: str
    status: str
    is_default: bool
    metadata_only: bool
    notes: str | None
    added_by: UUID | None
    reviewed_by: UUID | None
    created_at: datetime
    updated_at: datetime


def _domain_row(record: asyncpg.Record) -> DomainRow:
    return DomainRow(
        id=record["id"],
        tenant_id=record["tenant_id"],
        domain=record["domain"],
        trust_tier=record["trust_tier"],
        status=record["status"],
        is_default=record["is_default"],
        metadata_only=record["metadata_only"],
        notes=record["notes"],
        added_by=record["added_by"],
        reviewed_by=record["reviewed_by"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


# Interpolated into three queries below. It is a module constant, never
# caller input — every VALUE is a bound parameter ($1, $2, …). Bandit's B608
# cannot tell the difference, hence the nosec markers; the alternative is
# three copies of this list drifting apart.
_DOMAIN_COLUMNS = (
    "id, tenant_id, domain, trust_tier, status, is_default, metadata_only, "
    "notes, added_by, reviewed_by, created_at, updated_at"
)


# Queries are assembled once, at import time, from the constant above.
_SELECT_DOMAINS_SQL = f"SELECT {_DOMAIN_COLUMNS} FROM web_domains ORDER BY domain"  # nosec B608

_INSERT_DOMAIN_SQL = (
    "INSERT INTO web_domains "
    "(tenant_id, domain, trust_tier, status, metadata_only, notes, added_by) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7) "
    "ON CONFLICT (tenant_id, domain) DO NOTHING "
    f"RETURNING {_DOMAIN_COLUMNS}"  # nosec B608
)

_UPDATE_DOMAIN_SQL = (
    "UPDATE web_domains SET "
    "  trust_tier    = COALESCE($2, trust_tier), "
    "  status        = COALESCE($3, status), "
    "  metadata_only = COALESCE($4, metadata_only), "
    "  notes         = COALESCE($5, notes), "
    "  reviewed_by   = $6, "
    "  updated_at    = now() "
    "WHERE id = $1 "
    f"RETURNING {_DOMAIN_COLUMNS}"  # nosec B608
)


async def list_domains(conn: asyncpg.Connection) -> list[DomainRow]:
    """Every row the caller may see — RLS limits this to {caller tenant,
    GLOBAL}. Tenant rows sort last so the tenant-wins merge in
    `domain.allowlist` sees them after the shipped ones."""
    rows = await conn.fetch(_SELECT_DOMAINS_SQL)
    return [_domain_row(row) for row in rows]


async def insert_domain(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    domain: str,
    trust_tier: str,
    status: str,
    metadata_only: bool,
    notes: str | None,
    added_by: UUID,
) -> DomainRow | None:
    """Insert a tenant allowlist row; None when the tenant already has it."""
    record = await conn.fetchrow(
        _INSERT_DOMAIN_SQL,
        tenant_id,
        domain,
        trust_tier,
        status,
        metadata_only,
        notes,
        added_by,
    )
    return _domain_row(record) if record is not None else None


async def update_domain(
    conn: asyncpg.Connection,
    *,
    domain_id: UUID,
    trust_tier: str | None,
    status: str | None,
    metadata_only: bool | None,
    notes: str | None,
    reviewed_by: UUID,
) -> DomainRow | None:
    """Patch a tenant row. Shipped GLOBAL rows are not writable (RLS denies the
    UPDATE), so a tenant "disables a default" by inserting its own row."""
    record = await conn.fetchrow(
        _UPDATE_DOMAIN_SQL,
        domain_id,
        trust_tier,
        status,
        metadata_only,
        notes,
        reviewed_by,
    )
    return _domain_row(record) if record is not None else None


# ── web_pages ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PageRow:
    id: UUID
    url: str
    domain: str
    title: str | None
    fetched_at: datetime
    robots_ok: bool
    license_flag: str
    status: str
    snapshot_ref: str | None
    extract_ref: str | None
    skip_reason: str | None


async def get_page(conn: asyncpg.Connection, *, tenant_id: UUID, url_hash: str) -> PageRow | None:
    record = await conn.fetchrow(
        """
        SELECT id, url, domain, title, fetched_at, robots_ok, license_flag,
               status, snapshot_ref, extract_ref, skip_reason
        FROM web_pages
        WHERE tenant_id = $1 AND url_hash = $2
        """,
        tenant_id,
        url_hash,
    )
    if record is None:
        return None
    return PageRow(
        id=record["id"],
        url=record["url"],
        domain=record["domain"],
        title=record["title"],
        fetched_at=record["fetched_at"],
        robots_ok=record["robots_ok"],
        license_flag=record["license_flag"],
        status=record["status"],
        snapshot_ref=record["snapshot_ref"],
        extract_ref=record["extract_ref"],
        skip_reason=record["skip_reason"],
    )


async def upsert_page(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    url_hash: str,
    url: str,
    domain: str,
    title: str | None,
    robots_ok: bool,
    license_flag: str,
    status: str,
    http_status: int | None,
    content_type: str | None,
    byte_size: int | None,
    snapshot_ref: str | None,
    extract_ref: str | None,
    skip_reason: str | None,
) -> UUID:
    return await conn.fetchval(  # type: ignore[no-any-return]
        """
        INSERT INTO web_pages
            (tenant_id, url_hash, url, domain, title, fetched_at, robots_ok,
             license_flag, status, http_status, content_type, byte_size,
             snapshot_ref, extract_ref, skip_reason)
        VALUES ($1, $2, $3, $4, $5, now(), $6, $7, $8, $9, $10, $11, $12, $13, $14)
        ON CONFLICT (tenant_id, url_hash) DO UPDATE SET
            url = EXCLUDED.url,
            domain = EXCLUDED.domain,
            title = EXCLUDED.title,
            fetched_at = EXCLUDED.fetched_at,
            robots_ok = EXCLUDED.robots_ok,
            license_flag = EXCLUDED.license_flag,
            status = EXCLUDED.status,
            http_status = EXCLUDED.http_status,
            content_type = EXCLUDED.content_type,
            byte_size = EXCLUDED.byte_size,
            snapshot_ref = COALESCE(EXCLUDED.snapshot_ref, web_pages.snapshot_ref),
            extract_ref = COALESCE(EXCLUDED.extract_ref, web_pages.extract_ref),
            skip_reason = EXCLUDED.skip_reason
        RETURNING id
        """,
        tenant_id,
        url_hash,
        url,
        domain,
        title,
        robots_ok,
        license_flag,
        status,
        http_status,
        content_type,
        byte_size,
        snapshot_ref,
        extract_ref,
        skip_reason,
    )
