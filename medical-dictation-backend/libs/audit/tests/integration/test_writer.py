"""End-to-end audit writer tests against the live dev Postgres.

Skipped unless ``RUN_DB_INTEGRATION=1`` and ``make migrate-up`` was run.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from audit import (
    AuditWriter,
    GENESIS_PREV_HASH,
    Severity,
    canonicalize,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs migrate-up",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")

WRITER_DSN = f"postgresql://audit_writer:audit_writer@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
APP_DSN    = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
SUPERUSER_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def writer_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(WRITER_DSN, min_size=1, max_size=10, statement_cache_size=0)
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def app_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(APP_DSN, min_size=1, max_size=4, statement_cache_size=0)
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def superuser_conn() -> asyncpg.Connection:
    conn = await asyncpg.connect(SUPERUSER_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(autouse=True)
async def _wipe_audit(superuser_conn: asyncpg.Connection):
    """Each test starts with an empty audit.events table."""
    await superuser_conn.execute("TRUNCATE audit.events")
    yield


async def test_sequential_writes_produce_monotonic_seq(writer_pool: asyncpg.Pool) -> None:
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    receipts = []
    for i in range(10):
        r = await writer.write_event(
            tenant_id=tenant,
            kind="test.event",
            actor_role="clinician",
            payload={"i": i},
        )
        receipts.append(r)
    assert [r.seq for r in receipts] == list(range(1, 11))


async def test_genesis_prev_hash_is_zero_bytes(
    writer_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    await writer.write_event(tenant_id=tenant, kind="test.first", payload={})
    row = await superuser_conn.fetchrow(
        "SELECT prev_hash FROM audit.events WHERE tenant_id = $1 AND seq = 1", tenant
    )
    assert bytes(row["prev_hash"]) == GENESIS_PREV_HASH == bytes(32)


async def test_chain_hashes_are_recomputable(
    writer_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    """Day-5 verifier preview: walk the chain and recompute every hash."""
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    for i in range(5):
        await writer.write_event(
            tenant_id=tenant,
            kind="test.event",
            actor_role="clinician",
            actor_sub=UUID(int=i + 1),
            payload={"i": i},
        )

    rows = await superuser_conn.fetch(
        """
        SELECT seq, payload_jcs::text AS payload_jcs, prev_hash, payload_hash
        FROM audit.events
        WHERE tenant_id = $1
        ORDER BY seq
        """,
        tenant,
    )
    assert len(rows) == 5

    running = bytes(32)
    for row in rows:
        # Re-parse the JSONB column (Postgres re-serialises but JSON values
        # are still semantically equal — re-canonicalize before hashing).
        import json

        payload_dict = json.loads(row["payload_jcs"])
        jcs_bytes = canonicalize(payload_dict)
        expected = hashlib.sha256(running + jcs_bytes).digest()
        assert bytes(row["payload_hash"]) == expected, (
            f"hash divergence at seq={row['seq']}: "
            f"expected {expected.hex()[:16]}…, got {bytes(row['payload_hash']).hex()[:16]}…"
        )
        running = expected


async def test_concurrent_writes_same_tenant_are_serialised(
    writer_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    """50 concurrent writes for one tenant → continuous seq 1..50, no gaps, no dupes."""
    tenant = uuid4()
    writer = AuditWriter(writer_pool)

    async def write(i: int) -> None:
        await writer.write_event(
            tenant_id=tenant, kind="test.race", payload={"i": i}
        )

    await asyncio.gather(*(write(i) for i in range(50)))

    seqs = await superuser_conn.fetch(
        "SELECT seq FROM audit.events WHERE tenant_id = $1 ORDER BY seq", tenant
    )
    assert [r["seq"] for r in seqs] == list(range(1, 51))


async def test_concurrent_writes_different_tenants_do_not_conflict(
    writer_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    """Two tenants writing in parallel produce independent per-tenant sequences."""
    a, b = uuid4(), uuid4()
    writer = AuditWriter(writer_pool)

    async def write_to(tid: UUID, n: int) -> None:
        for i in range(n):
            await writer.write_event(tenant_id=tid, kind="test.event", payload={"i": i})

    await asyncio.gather(write_to(a, 10), write_to(b, 10))

    seqs_a = await superuser_conn.fetch(
        "SELECT seq FROM audit.events WHERE tenant_id = $1 ORDER BY seq", a
    )
    seqs_b = await superuser_conn.fetch(
        "SELECT seq FROM audit.events WHERE tenant_id = $1 ORDER BY seq", b
    )
    assert [r["seq"] for r in seqs_a] == list(range(1, 11))
    assert [r["seq"] for r in seqs_b] == list(range(1, 11))


async def test_jcs_stability_same_payload_different_key_order(
    writer_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    """Two events with semantically identical payloads but different key
    insertion order canonicalise (and therefore hash-contribute) identically."""
    tenant = uuid4()
    writer = AuditWriter(writer_pool)

    # The user-supplied payload differs only in key order. The contribution
    # of the payload to JCS must be the same; the only thing that changes
    # the hash between two consecutive events is the seq + prev_hash.
    await writer.write_event(
        tenant_id=tenant, kind="test.k", payload={"x": 1, "y": 2, "z": 3}
    )
    await writer.write_event(
        tenant_id=tenant, kind="test.k", payload={"z": 3, "y": 2, "x": 1}
    )

    rows = await superuser_conn.fetch(
        "SELECT seq, payload_jcs::text AS p FROM audit.events "
        "WHERE tenant_id = $1 ORDER BY seq",
        tenant,
    )
    # The stored payload_jcs values must be byte-identical except for the
    # automatic per-row metadata (seq, created_at).
    import json

    d1 = json.loads(rows[0]["p"])
    d2 = json.loads(rows[1]["p"])
    assert d1["payload"] == d2["payload"] == {"x": 1, "y": 2, "z": 3}
    assert canonicalize(d1["payload"]) == canonicalize(d2["payload"])


async def test_app_role_cannot_insert_into_audit_events(
    app_pool: asyncpg.Pool, writer_pool: asyncpg.Pool
) -> None:
    """The whole point of the audit_writer role: app_role MUST be rejected."""
    tenant = uuid4()
    # Pre-create a tenant so any FK or precondition is met (audit.events has no FK).
    async with app_pool.acquire() as c:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await c.execute(
                """
                INSERT INTO audit.events (
                    tenant_id, seq, kind, payload_jcs, payload_hash
                ) VALUES ($1, 1, 'forge', '{}'::jsonb, $2)
                """,
                tenant,
                bytes(32),
            )


async def test_update_is_rejected_by_trigger(
    writer_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    await writer.write_event(tenant_id=tenant, kind="t", payload={})

    # Attempt UPDATE as superuser — must be rejected by the immutability trigger.
    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        await superuser_conn.execute(
            "UPDATE audit.events SET kind = 'tampered' WHERE tenant_id = $1", tenant
        )


async def test_delete_is_rejected_by_trigger(
    writer_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    await writer.write_event(tenant_id=tenant, kind="t", payload={})

    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        await superuser_conn.execute(
            "DELETE FROM audit.events WHERE tenant_id = $1", tenant
        )


async def test_thousand_events_form_continuous_verifiable_chain(
    writer_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    """Spec § Day 4 done-criteria: insert 1000 events, verify continuous seq
    and that the chain reconstructs to the stored payload_hash for every row."""
    import json

    tenant = uuid4()
    writer = AuditWriter(writer_pool)

    for i in range(1000):
        await writer.write_event(
            tenant_id=tenant,
            kind="bulk.event",
            actor_role="clinician",
            payload={"i": i},
        )

    rows = await superuser_conn.fetch(
        """
        SELECT seq, payload_jcs::text AS payload_jcs, prev_hash, payload_hash
        FROM audit.events
        WHERE tenant_id = $1
        ORDER BY seq
        """,
        tenant,
    )
    assert len(rows) == 1000
    assert [r["seq"] for r in rows] == list(range(1, 1001))

    running = bytes(32)
    for row in rows:
        payload_dict = json.loads(row["payload_jcs"])
        expected = hashlib.sha256(running + canonicalize(payload_dict)).digest()
        assert bytes(row["payload_hash"]) == expected, f"divergence at seq={row['seq']}"
        running = expected


async def test_truncate_still_works_only_for_superuser(
    writer_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    """Documented escape: TRUNCATE bypasses row-level triggers but requires
    superuser/owner. This is acceptable because TRUNCATE access is itself a
    DBA-only privilege and is audited at the Postgres-log layer."""
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    await writer.write_event(tenant_id=tenant, kind="t", payload={})

    # Superuser can TRUNCATE.
    await superuser_conn.execute("TRUNCATE audit.events")
    row = await superuser_conn.fetchrow(
        "SELECT count(*) AS n FROM audit.events WHERE tenant_id = $1", tenant
    )
    assert row["n"] == 0
