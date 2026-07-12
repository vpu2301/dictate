"""Sprint-10 step-01 §6 behavioral contract — three-scope RLS.

The three-scope corpus (system / tenant / user) is enforced by Postgres
RLS, not service code. These tests are the authoritative guard for that
contract (spec: "the §6 tests are the guard, not code review"):

1. Any authenticated tenant connection SELECTs system rows.
2. Tenant A never SELECTs tenant B rows (phrases, snippets).
3. User A cannot INSERT/UPDATE a ``source='user'`` row owned by user B,
   even inside the same tenant.
4. A clinician-role connection cannot write ``source='tenant'`` rows;
   a tenant_admin connection can, only in its own tenant.
5. app_role cannot write ``source='system'`` rows; tenant_writer can.
6. GUCs are transaction-local: a reused pooled connection carries no
   ``app.user_id`` after the transaction ends.

Plus the step-01 schema guards: scope-coherence CHECKs, the per-scope
unique index (regression: 0023 shipped it non-UNIQUE; fixed in 0039),
and telemetry landing in the correct monthly partition.

Skipped unless ``RUN_DB_INTEGRATION=1``; needs ``make dev-up`` +
``make migrate-up`` (through 0039).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

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
WRITER_DSN = (
    f"postgresql://tenant_writer:tenant_writer@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
)
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")

# All test rows carry this marker so teardown can sweep them.
MARK = "itest-rls"


async def _set_user(conn: asyncpg.Connection, user_id: UUID, role: str) -> None:
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(user_id))
    await conn.execute("SELECT set_config('app.user_role', $1, true)", role)


def _phrase(tag: str) -> str:
    return f"{MARK} {tag} {uuid4().hex[:8]}"


@pytest.fixture
async def app_pool():
    pool = await create_pool(APP_DSN, application_name="rls-itest", min_size=1, max_size=2)
    yield pool
    await pool.close()


@pytest.fixture
async def tenant_a_users():
    """Two real tenant-A user subs — owner_user_id FKs to users(sub)."""
    su = await asyncpg.connect(SU_DSN)
    try:
        rows = await su.fetch(
            "SELECT sub FROM users WHERE tenant_id = $1 LIMIT 2", TENANT_A
        )
    finally:
        await su.close()
    if len(rows) < 2:
        pytest.skip("needs >= 2 seeded tenant-A users (make seed)")
    return rows[0]["sub"], rows[1]["sub"]


@pytest.fixture
async def writer_conn():
    conn = await asyncpg.connect(WRITER_DSN)
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
async def _sweep():
    yield
    su = await asyncpg.connect(SU_DSN)
    try:
        await su.execute(
            "DELETE FROM autocomplete_phrases WHERE phrase LIKE $1", f"{MARK}%"
        )
        await su.execute(
            "DELETE FROM autocomplete_snippets WHERE expansion LIKE $1", f"{MARK}%"
        )
        await su.execute(
            "DELETE FROM autocomplete_telemetry WHERE prefix_scrubbed LIKE $1",
            f"{MARK}%",
        )
    finally:
        await su.close()


# ── §6.1 system rows visible to every tenant ────────────────────────────


async def test_system_rows_visible_to_all_tenants(app_pool, writer_conn):
    text = _phrase("system")
    await writer_conn.execute(
        "INSERT INTO autocomplete_phrases (phrase, language, source) "
        "VALUES ($1, 'uk', 'system')",
        text,
    )
    for tid in (TENANT_A, TENANT_B):
        async with tenant_connection(app_pool, tid) as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM autocomplete_phrases WHERE phrase = $1", text
            )
        assert n == 1, f"tenant {tid} cannot see the system row"


# ── §6.2 tenant isolation ────────────────────────────────────────────────


async def test_tenant_b_cannot_select_tenant_a_rows(app_pool):
    text = _phrase("tenant-a")
    async with tenant_connection(app_pool, TENANT_A) as conn:
        await _set_user(conn, uuid4(), "tenant_admin")
        await conn.execute(
            "INSERT INTO autocomplete_phrases (tenant_id, phrase, language, source) "
            "VALUES ($1, $2, 'uk', 'tenant')",
            TENANT_A,
            text,
        )
    async with tenant_connection(app_pool, TENANT_B) as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM autocomplete_phrases WHERE phrase = $1", text
        )
    assert n == 0, "tenant B can read tenant A's phrase rows"

    trig = f"zt{uuid4().hex[:6]}"
    async with tenant_connection(app_pool, TENANT_A) as conn:
        await _set_user(conn, uuid4(), "tenant_admin")
        await conn.execute(
            "INSERT INTO autocomplete_snippets "
            "(tenant_id, trigger, expansion, language, source) "
            f"VALUES ($1, $2, '{MARK} exp', 'uk', 'tenant')",
            TENANT_A,
            trig,
        )
    async with tenant_connection(app_pool, TENANT_B) as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM autocomplete_snippets WHERE trigger = $1", trig
        )
    assert n == 0, "tenant B can read tenant A's snippet rows"


# ── §6.3 user rows only by their owner ───────────────────────────────────


async def test_user_b_cannot_write_user_a_rows(app_pool, tenant_a_users):
    user_a, user_b = tenant_a_users
    text = _phrase("user-a")
    async with tenant_connection(app_pool, TENANT_A) as conn:
        await _set_user(conn, user_a, "clinician")
        row_id = await conn.fetchval(
            "INSERT INTO autocomplete_phrases "
            "(tenant_id, owner_user_id, phrase, language, source) "
            "VALUES ($1, $2, $3, 'uk', 'user') RETURNING id",
            TENANT_A,
            user_a,
            text,
        )

    async with tenant_connection(app_pool, TENANT_A) as conn:
        await _set_user(conn, user_b, "clinician")
        # UPDATE must match zero rows (RESTRICTIVE USING filters it out).
        tag = await conn.execute(
            "UPDATE autocomplete_phrases SET enabled = FALSE WHERE id = $1", row_id
        )
        assert tag == "UPDATE 0", "user B updated user A's row"
        # INSERT claiming user A's ownership must be rejected outright.
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO autocomplete_phrases "
                "(tenant_id, owner_user_id, phrase, language, source) "
                "VALUES ($1, $2, $3, 'uk', 'user')",
                TENANT_A,
                user_a,
                _phrase("forged-owner"),
            )

    # Owner CAN update their own row (proves the zero-match above was RLS,
    # not a wrong id).
    async with tenant_connection(app_pool, TENANT_A) as conn:
        await _set_user(conn, user_a, "clinician")
        tag = await conn.execute(
            "UPDATE autocomplete_phrases SET enabled = FALSE WHERE id = $1", row_id
        )
        assert tag == "UPDATE 1"


# ── §6.4 tenant-source rows gated by role ────────────────────────────────


async def test_clinician_cannot_write_tenant_rows_admin_can(app_pool):
    async with tenant_connection(app_pool, TENANT_A) as conn:
        await _set_user(conn, uuid4(), "clinician")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO autocomplete_phrases (tenant_id, phrase, language, source) "
                "VALUES ($1, $2, 'uk', 'tenant')",
                TENANT_A,
                _phrase("clinician-tenant"),
            )

    async with tenant_connection(app_pool, TENANT_A) as conn:
        await _set_user(conn, uuid4(), "tenant_admin")
        await conn.execute(
            "INSERT INTO autocomplete_phrases (tenant_id, phrase, language, source) "
            "VALUES ($1, $2, 'uk', 'tenant')",
            TENANT_A,
            _phrase("admin-tenant"),
        )
        # …but only in their own tenant: claiming tenant B fails.
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO autocomplete_phrases (tenant_id, phrase, language, source) "
                "VALUES ($1, $2, 'uk', 'tenant')",
                TENANT_B,
                _phrase("cross-tenant"),
            )


# ── §6.5 system rows only by tenant_writer ───────────────────────────────


async def test_app_role_cannot_write_system_rows(app_pool, writer_conn):
    async with tenant_connection(app_pool, TENANT_A) as conn:
        await _set_user(conn, uuid4(), "tenant_admin")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO autocomplete_phrases (phrase, language, source) "
                "VALUES ($1, 'uk', 'system')",
                _phrase("app-system"),
            )
    # tenant_writer can (this is how the seed migration runs).
    await writer_conn.execute(
        "INSERT INTO autocomplete_phrases (phrase, language, source) "
        "VALUES ($1, 'uk', 'system')",
        _phrase("writer-system"),
    )


# ── §6.6 GUCs are transaction-local ──────────────────────────────────────


async def test_gucs_do_not_leak_across_pooled_connections():
    pool = await create_pool(
        APP_DSN, application_name="rls-itest-guc", min_size=1, max_size=1
    )
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            await _set_user(conn, uuid4(), "tenant_admin")
            assert await conn.fetchval(
                "SELECT current_setting('app.user_id', true)"
            )
        # max_size=1 → same physical connection, new transaction.
        async with pool.acquire() as conn:
            for guc in ("app.tenant_id", "app.user_id", "app.user_role"):
                val = await conn.fetchval(
                    "SELECT current_setting($1, true)", guc
                )
                assert val in (None, ""), f"{guc} leaked across transactions: {val!r}"
    finally:
        await pool.close()


# ── schema guards ────────────────────────────────────────────────────────


async def test_scope_coherence_checks_rejected():
    su = await asyncpg.connect(SU_DSN)
    try:
        # system row with a tenant_id
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await su.execute(
                "INSERT INTO autocomplete_phrases (tenant_id, phrase, language, source) "
                "VALUES ($1, $2, 'uk', 'system')",
                TENANT_A,
                _phrase("bad-system"),
            )
        # user row without an owner
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await su.execute(
                "INSERT INTO autocomplete_phrases (tenant_id, phrase, language, source) "
                "VALUES ($1, $2, 'uk', 'user')",
                TENANT_A,
                _phrase("bad-user"),
            )
    finally:
        await su.close()


async def test_duplicate_phrase_per_scope_rejected(writer_conn, tenant_a_users):
    """Regression: 0023's per-scope index was a plain btree (not UNIQUE)."""
    text = _phrase("dup")
    await writer_conn.execute(
        "INSERT INTO autocomplete_phrases (phrase, language, source) "
        "VALUES ($1, 'uk', 'system')",
        text,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await writer_conn.execute(
            "INSERT INTO autocomplete_phrases (phrase, language, source) "
            "VALUES ($1, 'uk', 'system')",
            text,
        )
    # Same text in a DIFFERENT scope is fine (tenant A user row).
    su = await asyncpg.connect(SU_DSN)
    try:
        await su.execute(
            "INSERT INTO autocomplete_phrases "
            "(tenant_id, owner_user_id, phrase, language, source) "
            "VALUES ($1, $2, $3, 'uk', 'user')",
            TENANT_A,
            tenant_a_users[0],
            text,
        )
    finally:
        await su.close()


async def test_telemetry_lands_in_current_month_partition(app_pool):
    rid = uuid4()
    async with tenant_connection(app_pool, TENANT_A) as conn:
        await conn.execute(
            "INSERT INTO autocomplete_telemetry "
            "(tenant_id, user_id, request_id, event_type, prefix_scrubbed, context_jsonb) "
            "VALUES ($1, $2, $3, 'shown_only', $4, '{}')",
            TENANT_A,
            uuid4(),
            rid,
            f"{MARK} partition probe",
        )
    su = await asyncpg.connect(SU_DSN)
    try:
        part = await su.fetchval(
            "SELECT tableoid::regclass::text FROM autocomplete_telemetry "
            "WHERE request_id = $1",
            rid,
        )
    finally:
        await su.close()
    expected = f"autocomplete_telemetry_{datetime.now(UTC).strftime('%Y_%m')}"
    assert part == expected, f"row landed in {part}, expected {expected}"
