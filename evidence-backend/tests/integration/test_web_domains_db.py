"""EVA-S04 verification: migration 0070 (`web_domains`, `web_pages`).

Env-gated: RUN_DB_INTEGRATION=1 with the platform dev stack up and migration
0070 applied (make dev-up && make migrate-up in ../medical-dictation-backend).

Connects as `app_role` — the standing the services actually have — so every
probe measures the real enforcement boundary, not a superuser bypass.

What is proven here that unit tests cannot:

* the shipped allowlist is readable by every tenant but writable by none;
* a tenant row genuinely shadows a shipped row *through RLS*, not just in the
  Python merge;
* `web_pages` is strictly tenant-scoped (no global read, unlike the corpus);
* the hostname CHECK rejects the shapes the fetcher's allowlist matcher would
  mis-handle.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="DB integration tests are env-gated (RUN_DB_INTEGRATION=1)",
)

DSN = os.environ.get(
    "EVIDENCE_TEST_DSN", "postgresql://app_role:app_role@localhost:5432/medical_dictation"
)

GLOBAL_TENANT = "00000000-0000-0000-0000-000000000000"
TENANT_A = "00000000-0000-0000-0000-00000000000a"
TENANT_B = "00000000-0000-0000-0000-00000000000b"


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    connection = await asyncpg.connect(DSN)
    try:
        yield connection
    finally:
        await connection.close()


async def scoped(connection: asyncpg.Connection, tenant: str) -> None:
    """Session-scoped for probing convenience — services use the
    transaction-local `tenant_connection`."""
    await connection.execute("SELECT set_config('app.tenant_id', $1, false)", tenant)


# ── the shipped allowlist ───────────────────────────────────────────────


async def test_shipped_allowlist_is_seeded_and_enabled(conn: asyncpg.Connection) -> None:
    await scoped(conn, TENANT_A)

    rows = await conn.fetch(
        "SELECT domain, trust_tier, status, metadata_only FROM web_domains "
        "WHERE tenant_id = $1 ORDER BY domain",
        GLOBAL_TENANT,
    )

    domains = {r["domain"] for r in rows}
    assert {"who.int", "cdc.gov", "moz.gov.ua", "nice.org.uk"} <= domains
    assert all(r["status"] == "enabled" for r in rows)
    # Journals and PubMed abstract pages are cited, never body-extracted.
    by_domain = {r["domain"]: r for r in rows}
    assert by_domain["nejm.org"]["metadata_only"] is True
    assert by_domain["who.int"]["metadata_only"] is False


async def test_every_tenant_reads_the_shipped_allowlist(conn: asyncpg.Connection) -> None:
    for tenant in (TENANT_A, TENANT_B):
        await scoped(conn, tenant)
        count = await conn.fetchval(
            "SELECT count(*) FROM web_domains WHERE tenant_id = $1", GLOBAL_TENANT
        )
        assert count and count > 20


async def test_a_tenant_cannot_write_a_shipped_row(conn: asyncpg.Connection) -> None:
    """The shipped table stays a reviewed artifact. RLS filters the GLOBAL row
    out of the UPDATE, so the write reports zero rows rather than failing —
    a cross-scope write reads as not-found (rule PD3)."""
    await scoped(conn, TENANT_A)

    result = await conn.execute(
        "UPDATE web_domains SET status = 'disabled' WHERE tenant_id = $1 AND domain = 'who.int'",
        GLOBAL_TENANT,
    )

    assert result == "UPDATE 0"


async def test_a_tenant_cannot_insert_into_the_global_scope(conn: asyncpg.Connection) -> None:
    await scoped(conn, TENANT_A)

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await conn.execute(
            "INSERT INTO web_domains (tenant_id, domain, trust_tier) "
            "VALUES ($1, 'evil.example.com', 'other')",
            GLOBAL_TENANT,
        )


# ── tenant rows ─────────────────────────────────────────────────────────


async def test_tenant_row_shadows_a_shipped_row(conn: asyncpg.Connection) -> None:
    """How a tenant disables a default: insert its own row for the same
    domain. Both rows are visible; the resolver prefers the tenant's."""
    await scoped(conn, TENANT_A)
    await conn.execute("DELETE FROM web_domains WHERE tenant_id = $1", TENANT_A)
    await conn.execute(
        "INSERT INTO web_domains (tenant_id, domain, trust_tier, status) "
        "VALUES ($1, 'nejm.org', 'journal', 'disabled')",
        TENANT_A,
    )

    rows = await conn.fetch("SELECT tenant_id, status FROM web_domains WHERE domain = 'nejm.org'")

    by_tenant = {str(r["tenant_id"]): r["status"] for r in rows}
    assert by_tenant[GLOBAL_TENANT] == "enabled"
    assert by_tenant[TENANT_A] == "disabled"

    await conn.execute("DELETE FROM web_domains WHERE tenant_id = $1", TENANT_A)


async def test_one_tenants_allowlist_is_invisible_to_another(
    conn: asyncpg.Connection,
) -> None:
    await scoped(conn, TENANT_A)
    await conn.execute(
        "INSERT INTO web_domains (tenant_id, domain, trust_tier) "
        "VALUES ($1, 'tenant-a-only.example.org', 'other') "
        "ON CONFLICT DO NOTHING",
        TENANT_A,
    )

    await scoped(conn, TENANT_B)
    visible = await conn.fetchval(
        "SELECT count(*) FROM web_domains WHERE domain = 'tenant-a-only.example.org'"
    )
    assert visible == 0

    await scoped(conn, TENANT_A)
    await conn.execute("DELETE FROM web_domains WHERE tenant_id = $1", TENANT_A)


@pytest.mark.parametrize(
    "bad_domain",
    [
        "https://who.int",  # scheme
        "who.int/guidelines",  # path
        "who.int:443",  # port
        "*.who.int",  # wildcard
        "who",  # no TLD
        "WHO.INT",  # uppercase (the matcher casefolds; the column must not)
        "-who.int",  # leading hyphen
    ],
)
async def test_hostname_check_rejects_non_hostnames(
    conn: asyncpg.Connection, bad_domain: str
) -> None:
    """A malformed row cannot widen the allowlist by accident: the matcher
    compares hostnames, so anything that is not one must never be stored."""
    await scoped(conn, TENANT_A)

    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO web_domains (tenant_id, domain, trust_tier) VALUES ($1, $2, 'other')",
            TENANT_A,
            bad_domain,
        )


# ── web_pages ───────────────────────────────────────────────────────────


async def test_web_pages_are_strictly_tenant_scoped(conn: asyncpg.Connection) -> None:
    """Unlike the corpus, cached pages have NO global read: which tenant
    looked at what is itself information."""
    page_hash = uuid.uuid4().hex
    await scoped(conn, TENANT_A)
    await conn.execute(
        "INSERT INTO web_pages (tenant_id, url_hash, url, domain, status) "
        "VALUES ($1, $2, 'https://who.int/x', 'who.int', 'ok')",
        TENANT_A,
        page_hash,
    )

    await scoped(conn, TENANT_B)
    visible = await conn.fetchval("SELECT count(*) FROM web_pages WHERE url_hash = $1", page_hash)
    assert visible == 0

    await scoped(conn, TENANT_A)
    await conn.execute("DELETE FROM web_pages WHERE url_hash = $1", page_hash)


async def test_web_pages_status_check_rejects_an_unknown_outcome(
    conn: asyncpg.Connection,
) -> None:
    await scoped(conn, TENANT_A)

    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO web_pages (tenant_id, url_hash, url, domain, status) "
            "VALUES ($1, $2, 'https://who.int/y', 'who.int', 'probably_fine')",
            TENANT_A,
            uuid.uuid4().hex,
        )


async def test_url_hash_is_unique_per_tenant_not_globally(
    conn: asyncpg.Connection,
) -> None:
    """Two tenants may each cache the same page — separately."""
    page_hash = uuid.uuid4().hex
    for tenant in (TENANT_A, TENANT_B):
        await scoped(conn, tenant)
        await conn.execute(
            "INSERT INTO web_pages (tenant_id, url_hash, url, domain, status) "
            "VALUES ($1, $2, 'https://who.int/z', 'who.int', 'ok')",
            tenant,
            page_hash,
        )
    for tenant in (TENANT_A, TENANT_B):
        await scoped(conn, tenant)
        await conn.execute("DELETE FROM web_pages WHERE url_hash = $1", page_hash)


async def test_both_new_tables_have_rls_forced(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname IN ('web_domains', 'web_pages')"
    )
    assert len(rows) == 2
    for row in rows:
        assert row["relrowsecurity"], f"{row['relname']} has no RLS"
        assert row["relforcerowsecurity"], f"{row['relname']} does not FORCE RLS"
