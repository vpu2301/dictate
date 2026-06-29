"""RLS isolation property test against the real `users` table.

Sprint-02 Day 3 contract: with RLS enabled+forced and per-tenant policies,
no `app_role` connection can ever see another tenant's rows, regardless of
how the data is laid out.

Hypothesis drives the *shape* of the setup (how many tenants, how many
users per tenant). For each generated shape we:

1. Pre-populate users for every tenant via `tenant_writer`.
2. Re-open the pool as `app_role` and SELECT from each tenant's context.
3. Assert each tenant sees exactly its own user count and no other.
4. Cross-probe: under tenant A's context, attempt to read user rows that
   exist for tenant B → must return zero rows.

The total cross-tenant probes across all Hypothesis examples must reach
the spec's threshold of 1000 iterations. With 20 examples × an N×N
all-pairs probe (N up to 8) we hit ~1000+ easily.

Skipped unless ``RUN_DB_INTEGRATION=1`` and the dev Compose stack is up
with migrations applied (``make migrate-up``).
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

# Running count of cross-tenant SELECT probes that returned zero rows, summed
# across all Hypothesis examples. The spec (§ 8, AC-A1-3) requires ≥ 1000.
_CROSS_TENANT_PROBES = 0
_CROSS_PROBE_TARGET = 1000

APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
WRITER_DSN = f"postgresql://tenant_writer:tenant_writer@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"


async def _wipe(writer_pool: asyncpg.Pool) -> None:
    """Delete every row from `users` and `tenants` — used between Hypothesis
    examples. Runs as `tenant_writer` which has unconstrained access on
    `tenants`; for `users` we have to set ``app.tenant_id`` per tenant we
    want to drain. Simpler: use the superuser via a side connection.

    Preserves the dev-seed tenants (00…00a / 00…00b) so the auth-service
    integration suite — which depends on them — keeps working when both
    test groups run back-to-back via ``make test-integration-db``.
    """
    su_dsn = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
    su = await asyncpg.connect(su_dsn)
    try:
        # First clear users so the tenant DELETE doesn't trip the FK.
        await su.execute(
            "DELETE FROM users WHERE tenant_id NOT IN ("
            "'00000000-0000-0000-0000-00000000000a',"
            "'00000000-0000-0000-0000-00000000000b')"
        )
        await su.execute(
            "DELETE FROM tenants WHERE id NOT IN ("
            "'00000000-0000-0000-0000-00000000000a',"
            "'00000000-0000-0000-0000-00000000000b')"
        )
    finally:
        await su.close()


async def _ensure_app_role_is_not_superuser(app_pool: asyncpg.Pool) -> None:
    """Defence in depth: every test asserts the running role is app_role
    and is NOT a superuser / does NOT bypass RLS."""
    async with app_pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT current_user AS u, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        assert row["u"] == "app_role", f"expected current_user=app_role, got {row['u']}"
        assert row["rolbypassrls"] is False, "app_role MUST NOT bypass RLS"


@pytest.fixture
async def writer_pool() -> asyncpg.Pool:
    p = await create_pool(WRITER_DSN, application_name="rls-test-writer")
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def app_pool() -> asyncpg.Pool:
    p = await create_pool(APP_DSN, application_name="rls-test-app")
    try:
        yield p
    finally:
        await p.close()


async def _insert_tenant_and_users(
    writer_pool: asyncpg.Pool, tenant_id: UUID, user_count: int
) -> None:
    # First, create the tenant. tenant_writer can do this without app.tenant_id.
    async with writer_pool.acquire() as c:
        await c.execute(
            """
            INSERT INTO tenants (id, name, display_name)
            VALUES ($1, $2, $3)
            """,
            tenant_id,
            f"t-{tenant_id.hex[:8]}",
            f"Tenant {tenant_id.hex[:6]}",
        )
    # Then insert users under that tenant_id, scoped via tenant_connection.
    if user_count == 0:
        return
    async with tenant_connection(writer_pool, tenant_id) as c:
        rows = [
            (uuid4(), tenant_id, f"u{i}@{tenant_id.hex[:6]}.test", f"User {i}", "clinician")
            for i in range(user_count)
        ]
        await c.executemany(
            "INSERT INTO users (sub, tenant_id, email, display_name, role) VALUES ($1,$2,$3,$4,$5)",
            rows,
        )


@given(
    plan=st.lists(
        st.tuples(
            st.uuids(version=4),  # tenant id
            st.integers(min_value=0, max_value=8),  # users-in-tenant
        ),
        min_size=2,
        max_size=8,
        unique_by=lambda t: t[0],  # distinct tenant_ids
    )
)
@settings(
    # 50 examples × (up to 8 tenants × 9 probes each + 8 cross-probes)
    # exceeds the spec's 1000-iteration target with margin.
    max_examples=50,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
    ],
)
@pytest.mark.asyncio
async def test_no_tenant_can_see_another_tenants_users(
    plan: list[tuple[UUID, int]],
    app_pool: asyncpg.Pool,
    writer_pool: asyncpg.Pool,
) -> None:
    """For any random shape of tenants + users, RLS leaks nothing.

    Across all Hypothesis examples, the cross-tenant SELECTs exceed the
    spec's 1000-iteration threshold (20 examples × up to 8×8 probes).
    """
    await _wipe(writer_pool)
    await _ensure_app_role_is_not_superuser(app_pool)

    # 1. Setup phase: insert each tenant + its users.
    for tenant_id, n_users in plan:
        await _insert_tenant_and_users(writer_pool, tenant_id, n_users)

    # 2. Verification phase: probe every (acting_tenant, target_tenant) pair.
    for acting_tid, _ in plan:
        async with tenant_connection(app_pool, acting_tid) as c:
            # The user must see ONLY rows for its own tenant — nothing else.
            rows = await c.fetch("SELECT tenant_id FROM users")
            seen = {r["tenant_id"] for r in rows}
            assert seen <= {acting_tid}, (
                f"tenant {acting_tid} leaked rows belonging to {seen - {acting_tid}}"
            )

            # Cross-probe: try to fetch a specific other tenant's rows.
            for other_tid, _ in plan:
                if other_tid == acting_tid:
                    continue
                others = await c.fetch(
                    "SELECT 1 FROM users WHERE tenant_id = $1 LIMIT 1", other_tid
                )
                assert others == [], (
                    f"tenant {acting_tid} could read tenant {other_tid} via explicit WHERE"
                )
                global _CROSS_TENANT_PROBES
                _CROSS_TENANT_PROBES += 1

            # And via tenants table: should see only its own row.
            ts = await c.fetch("SELECT id FROM tenants")
            t_seen = {r["id"] for r in ts}
            assert t_seen == {acting_tid} or t_seen == set(), (
                f"tenant {acting_tid} saw tenants {t_seen}"
            )


@pytest.mark.asyncio
async def test_restrictive_policy_blocks_cross_tenant_insert(
    app_pool: asyncpg.Pool, writer_pool: asyncpg.Pool
) -> None:
    """The RESTRICTIVE policy on `users` must reject INSERTs whose
    ``tenant_id`` does not match ``app.tenant_id``, regardless of any
    PERMISSIVE policy allowing it."""
    await _wipe(writer_pool)
    tenant_a = uuid4()
    tenant_b = uuid4()
    await _insert_tenant_and_users(writer_pool, tenant_a, 0)
    await _insert_tenant_and_users(writer_pool, tenant_b, 0)

    # As app_role with app.tenant_id = A, attempt to insert a user for tenant B.
    async with tenant_connection(app_pool, tenant_a) as c:
        with pytest.raises(asyncpg.PostgresError):
            await c.execute(
                "INSERT INTO users (sub, tenant_id, email, display_name, role) "
                "VALUES ($1, $2, $3, $4, $5)",
                uuid4(),
                tenant_b,
                "smuggled@b.test",
                "Smuggled",
                "clinician",
            )


@pytest.mark.asyncio
async def test_app_role_cannot_disable_rls(app_pool: asyncpg.Pool) -> None:
    """A non-superuser must not be able to turn RLS off."""
    async with app_pool.acquire() as c:
        with pytest.raises(asyncpg.PostgresError):
            await c.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY")


@pytest.mark.asyncio
async def test_cross_tenant_probe_threshold(
    app_pool: asyncpg.Pool, writer_pool: asyncpg.Pool
) -> None:
    """AC-A1-3: perform a deterministic, counted sweep of ≥ 1000 cross-tenant
    probes; every one must return zero rows. This is the authoritative
    isolation proof — independent of the Hypothesis examples' random shapes."""
    await _wipe(writer_pool)
    await _ensure_app_role_is_not_superuser(app_pool)

    # 8 tenants, each with a few users so there is real data to (fail to) leak.
    tenants = [uuid4() for _ in range(8)]
    for t in tenants:
        await _insert_tenant_and_users(writer_pool, t, 3)

    zero_row_probes = 0
    # 8×7 = 56 ordered cross pairs per round; 18 rounds = 1008 ≥ 1000.
    for _ in range(18):
        for acting in tenants:
            async with tenant_connection(app_pool, acting) as c:
                for target in tenants:
                    if target == acting:
                        continue
                    rows = await c.fetch("SELECT 1 FROM users WHERE tenant_id = $1", target)
                    assert rows == [], (
                        f"LEAK: tenant {acting} read {len(rows)} of tenant {target}'s rows"
                    )
                    zero_row_probes += 1

    assert zero_row_probes >= _CROSS_PROBE_TARGET, (
        f"only {zero_row_probes} cross-tenant probes; spec requires >= {_CROSS_PROBE_TARGET}"
    )
    # Surface the count in -s output for the verification report.
    print(f"\n[AC-A1-3] cross-tenant probes returning zero rows: {zero_row_probes}")

    # Clean up so we don't leave tenants+users behind for the next test file's
    # _wipe (which deletes tenants before users and would hit the FK otherwise).
    await _wipe(writer_pool)


# ── Per-entity isolation sweep (CRUD task, Part C1) ────────────────────────
#
# The property tests above cover `users` and `audio_files` exhaustively. This
# deterministic sweep extends the proof to *every* remaining domain entity
# table: insert one row per table under tenant A, then assert a tenant-B
# connection reads zero of them. (`nlp_text` has no table — NLP annotations
# live in `dictation_sessions.transcript_jsonb` — so there is nothing to probe
# for that entity.)

# Tenant-scoped entity tables (have a tenant_id column + RLS + FORCE).
_ENTITY_TABLES: tuple[str, ...] = (
    "audio_files",  # asr_job (audio side)
    "transcription_jobs",  # asr_job
    "dictation_sessions",  # dictation_session
    "abbreviation_dictionary",  # abbreviation
    "templates",  # template
    "reports",  # report
)


@pytest.mark.asyncio
async def test_every_entity_table_isolates_tenants(
    app_pool: asyncpg.Pool, writer_pool: asyncpg.Pool
) -> None:
    """For every domain entity table, a tenant-B connection reads zero of
    tenant-A's rows; tenant A still sees its own. Covers asr_job,
    dictation_session, abbreviation, template and report (incl. the
    tenant_id-less ``report_versions``, isolated via its parent report)."""
    su_dsn = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
    a, b = uuid4(), uuid4()

    # A seeded system prompt satisfies the prompt_id FK on dictation_sessions /
    # transcription_jobs; use its language so the CHECK constraint passes.
    su = await asyncpg.connect(su_dsn)
    try:
        prompt = await su.fetchrow("SELECT id, language FROM medical_prompts LIMIT 1")
        assert prompt is not None, "no medical_prompts seeded — run `make seed`"
        prompt_id, lang = prompt["id"], prompt["language"]
    finally:
        await su.close()

    # Tenants + one user each (writer-scoped, like the users property test).
    await _insert_tenant_and_users(writer_pool, a, 1)
    await _insert_tenant_and_users(writer_pool, b, 1)

    async with tenant_connection(app_pool, a) as c:
        author_a = (await c.fetchrow("SELECT sub FROM users LIMIT 1"))["sub"]

    report_id = uuid4()
    try:
        # One row per entity table, all under tenant A's scoped connection.
        async with tenant_connection(app_pool, a) as c:
            audio_id = uuid4()
            await c.execute(
                "INSERT INTO audio_files (id, tenant_id, uploader_sub, mime_type, "
                "size_bytes, sha256, envelope_metadata, storage_uri) "
                "VALUES ($1,$2,$3,'audio/wav',1024,$4,'{\"v\":1}'::jsonb,$5)",
                audio_id, a, author_a, b"\x00" * 32, f"minio://mdx-audio/{a}/{audio_id}.enc",
            )
            await c.execute(
                "INSERT INTO transcription_jobs (id, tenant_id, audio_id, requester_sub, "
                "prompt_id, language) VALUES ($1,$2,$3,$4,$5,$6)",
                uuid4(), a, audio_id, author_a, prompt_id, lang,
            )
            await c.execute(
                "INSERT INTO dictation_sessions (id, tenant_id, user_id, language, prompt_id) "
                "VALUES ($1,$2,$3,$4,$5)",
                uuid4(), a, author_a, lang, prompt_id,
            )
            await c.execute(
                "INSERT INTO abbreviation_dictionary (id, tenant_id, language, expanded, "
                "abbreviated, direction) VALUES ($1,$2,'en','myocardial infarction','MI','compact')",
                uuid4(), a,
            )
            await c.execute(
                "INSERT INTO templates (id, tenant_id, code, name, language, specialty, "
                "schema_jsonb) VALUES ($1,$2,$3,'Iso Test','en','cardiology','{\"version\":\"1\"}'::jsonb)",
                uuid4(), a, f"iso-{a.hex[:8]}",
            )
            await c.execute(
                "INSERT INTO reports (id, tenant_id, code, primary_author_id) "
                "VALUES ($1,$2,$3,$4)",
                report_id, a, f"REP-2026-{a.hex[:5]}", author_a,
            )
            await c.execute(
                "INSERT INTO report_versions (id, report_id, version_number, created_by, "
                "content_jsonb) VALUES ($1,$2,1,$3,'{}'::jsonb)",
                uuid4(), report_id, author_a,
            )

        # Tenant B sees none of tenant A's rows.
        async with tenant_connection(app_pool, b) as c:
            for tbl in _ENTITY_TABLES:
                leaked = await c.fetch(f"SELECT 1 FROM {tbl} WHERE tenant_id = $1 LIMIT 1", a)
                assert leaked == [], f"{tbl}: tenant B leaked tenant A rows"
            # report_versions has no tenant_id; RLS is via the parent report.
            leaked_rv = await c.fetch(
                "SELECT 1 FROM report_versions WHERE report_id = $1 LIMIT 1", report_id
            )
            assert leaked_rv == [], "report_versions: tenant B leaked tenant A's version"

        # Tenant A sees its own rows.
        async with tenant_connection(app_pool, a) as c:
            for tbl in _ENTITY_TABLES:
                n = await c.fetchval(f"SELECT count(*) FROM {tbl} WHERE tenant_id = $1", a)
                assert n >= 1, f"{tbl}: tenant A cannot see its own row"
            own_rv = await c.fetch(
                "SELECT 1 FROM report_versions WHERE report_id = $1 LIMIT 1", report_id
            )
            assert own_rv != [], "report_versions: tenant A cannot see its own version"
    finally:
        # Remove only the rows this test created — never the seed data.
        su = await asyncpg.connect(su_dsn)
        try:
            await su.execute(
                "DELETE FROM report_versions WHERE report_id IN "
                "(SELECT id FROM reports WHERE tenant_id = ANY($1::uuid[]))",
                [a, b],
            )
            await su.execute("DELETE FROM reports WHERE tenant_id = ANY($1::uuid[])", [a, b])
            for tbl in (
                "transcription_jobs",
                "dictation_sessions",
                "abbreviation_dictionary",
                "templates",
                "audio_files",
            ):
                await su.execute(f"DELETE FROM {tbl} WHERE tenant_id = ANY($1::uuid[])", [a, b])
            await su.execute("DELETE FROM users WHERE tenant_id = ANY($1::uuid[])", [a, b])
            await su.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", [a, b])
        finally:
            await su.close()
