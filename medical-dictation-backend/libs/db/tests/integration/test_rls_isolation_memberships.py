"""RLS isolation for ``tenant_memberships`` (Sprint 12).

Contract: an ``app_role`` connection scoped to tenant B must never see the
membership rows of tenant A; writes to memberships are ``tenant_writer``-only
(app_role holds SELECT only). Mirrors ``test_rls_isolation.py`` fixtures.

Skipped unless ``RUN_DB_INTEGRATION=1`` with the dev stack up + migrated.
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from db import create_pool, tenant_connection

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs `make dev-up && make migrate-up`",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")

APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
WRITER_DSN = f"postgresql://tenant_writer:tenant_writer@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"


@pytest.fixture
async def writer_pool() -> asyncpg.Pool:
    p = await create_pool(WRITER_DSN, application_name="rls-mem-writer")
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def app_pool() -> asyncpg.Pool:
    p = await create_pool(APP_DSN, application_name="rls-mem-app")
    try:
        yield p
    finally:
        await p.close()


async def _seed(writer_pool: asyncpg.Pool, tenant_id, name: str) -> None:
    async with writer_pool.acquire() as c:
        await c.execute(
            "INSERT INTO tenants (id, name, display_name, slug) VALUES ($1,$2,$3,$2)",
            tenant_id, name, name.title(),
        )
    # Membership writes are tenant_writer-only; scope via app.tenant_id.
    async with tenant_connection(writer_pool, tenant_id) as c:
        await c.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_sub, role) VALUES ($1,$2,'owner')",
            tenant_id, uuid4(),
        )


@pytest.mark.asyncio
async def test_memberships_isolated_across_tenants(
    app_pool: asyncpg.Pool, writer_pool: asyncpg.Pool
) -> None:
    a, b = uuid4(), uuid4()
    su_dsn = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
    try:
        await _seed(writer_pool, a, f"mem-a-{a.hex[:8]}")
        await _seed(writer_pool, b, f"mem-b-{b.hex[:8]}")

        # Tenant B's app_role connection sees zero of tenant A's memberships.
        async with tenant_connection(app_pool, b) as c:
            leaked = await c.fetch(
                "SELECT 1 FROM tenant_memberships WHERE tenant_id = $1", a
            )
            assert leaked == [], "tenant B leaked tenant A memberships"

        # Tenant A sees exactly its own.
        async with tenant_connection(app_pool, a) as c:
            own = await c.fetch("SELECT tenant_id FROM tenant_memberships")
            assert {r["tenant_id"] for r in own} == {a}

        # app_role holds SELECT only — an INSERT must be refused by grants/RLS.
        async with tenant_connection(app_pool, a) as c:
            with pytest.raises(asyncpg.PostgresError):
                await c.execute(
                    "INSERT INTO tenant_memberships (tenant_id, user_sub, role) "
                    "VALUES ($1,$2,'admin')",
                    a, uuid4(),
                )
    finally:
        su = await asyncpg.connect(su_dsn)
        try:
            await su.execute(
                "DELETE FROM tenant_memberships WHERE tenant_id = ANY($1::uuid[])", [a, b]
            )
            await su.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", [a, b])
        finally:
            await su.close()
