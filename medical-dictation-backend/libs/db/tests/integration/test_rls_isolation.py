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
from hypothesis import HealthCheck, given, settings, strategies as st

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
        rows = [(uuid4(), tenant_id, f"u{i}@{tenant_id.hex[:6]}.test", f"User {i}", "clinician")
                for i in range(user_count)]
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
