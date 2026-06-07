"""RLS property tests for sprint-03 tables: audio_files + transcription_jobs.

Extends the sprint-02 suite to cover the new PHI-bearing tables. Audio
metadata leakage is a regulatory-grade incident; this suite gates merges.

Skipped unless RUN_DB_INTEGRATION=1.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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
    p = await create_pool(WRITER_DSN, application_name="rls-asr-test-writer")
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def app_pool() -> asyncpg.Pool:
    p = await create_pool(APP_DSN, application_name="rls-asr-test-app")
    try:
        yield p
    finally:
        await p.close()


async def _wipe(writer_pool: asyncpg.Pool) -> None:
    """Drop sprint-03 test data while preserving the dev-seed tenants."""
    su_dsn = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
    su = await asyncpg.connect(su_dsn)
    try:
        await su.execute("DELETE FROM transcription_jobs")
        await su.execute("DELETE FROM audio_files")
        await su.execute(
            "DELETE FROM tenants WHERE id NOT IN ("
            "'00000000-0000-0000-0000-00000000000a',"
            "'00000000-0000-0000-0000-00000000000b')"
        )
    finally:
        await su.close()


async def _make_tenant(writer_pool: asyncpg.Pool, tid: UUID) -> None:
    async with writer_pool.acquire() as c:
        await c.execute(
            "INSERT INTO tenants (id, name, display_name) VALUES ($1, $2, $3)",
            tid,
            f"t-{tid.hex[:8]}",
            f"Tenant {tid.hex[:6]}",
        )


async def _insert_audio(app_pool: asyncpg.Pool, tid: UUID, count: int) -> list[UUID]:
    ids: list[UUID] = []
    if count == 0:
        return ids
    async with tenant_connection(app_pool, tid) as c:
        rows = []
        for _ in range(count):
            aid = uuid4()
            ids.append(aid)
            rows.append(
                (
                    aid,
                    tid,
                    uuid4(),
                    "audio/wav",
                    1024,
                    b"\x00" * 32,
                    '{"v":1}',
                    f"minio://mdx-audio/{tid}/{aid}.enc",
                )
            )
        await c.executemany(
            """
            INSERT INTO audio_files
                (id, tenant_id, uploader_sub, mime_type, size_bytes,
                 sha256, envelope_metadata, storage_uri)
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8)
            """,
            rows,
        )
    return ids


@given(
    plan=st.lists(
        st.tuples(st.uuids(version=4), st.integers(min_value=0, max_value=5)),
        min_size=2,
        max_size=5,
        unique_by=lambda t: t[0],
    )
)
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
    ],
)
async def test_audio_files_rls_isolation(
    plan: list[tuple[UUID, int]],
    app_pool: asyncpg.Pool,
    writer_pool: asyncpg.Pool,
) -> None:
    """No tenant can read another's `audio_files` rows."""
    await _wipe(writer_pool)
    for tid, _ in plan:
        await _make_tenant(writer_pool, tid)
    for tid, n in plan:
        await _insert_audio(app_pool, tid, n)

    for acting_tid, _ in plan:
        async with tenant_connection(app_pool, acting_tid) as c:
            rows = await c.fetch("SELECT tenant_id FROM audio_files")
            seen = {r["tenant_id"] for r in rows}
            assert seen <= {acting_tid}, (
                f"tenant {acting_tid} leaked audio_files for {seen - {acting_tid}}"
            )

            for other_tid, _ in plan:
                if other_tid == acting_tid:
                    continue
                others = await c.fetch(
                    "SELECT 1 FROM audio_files WHERE tenant_id = $1 LIMIT 1",
                    other_tid,
                )
                assert others == [], (
                    f"explicit cross-tenant SELECT leaked rows: {acting_tid} → {other_tid}"
                )


async def test_audio_files_restrictive_policy_blocks_cross_tenant_insert(
    app_pool: asyncpg.Pool, writer_pool: asyncpg.Pool
) -> None:
    """RESTRICTIVE policy must reject inserts whose tenant_id != app.tenant_id."""
    await _wipe(writer_pool)
    a, b = uuid4(), uuid4()
    await _make_tenant(writer_pool, a)
    await _make_tenant(writer_pool, b)

    async with tenant_connection(app_pool, a) as c:
        with pytest.raises(asyncpg.PostgresError):
            await c.execute(
                """
                INSERT INTO audio_files
                    (id, tenant_id, uploader_sub, mime_type, size_bytes,
                     sha256, envelope_metadata, storage_uri)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
                """,
                uuid4(),
                b,  # smuggled
                uuid4(),
                "audio/wav",
                1024,
                b"\x00" * 32,
                '{"v":1}',
                "minio://mdx-audio/smuggled.enc",
            )
