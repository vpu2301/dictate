"""Verifier integration tests — including the three adversarial corruption
patterns mandated by spec § 1.5.

A clean chain verifies; a tampered chain reports the first divergence and
its reason. The verifier runs only SELECT queries (read-only transaction).

To simulate tampering we have to bypass the immutability trigger. We do
that by disabling the trigger as superuser, mutating, re-enabling — the
exact mechanic an attacker with DBA access could use.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from audit import (
    AuditVerifier,
    AuditWriter,
    DivergenceReason,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs migrate-up",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")

WRITER_DSN = f"postgresql://audit_writer:audit_writer@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
READER_DSN = f"postgresql://audit_reader:audit_reader@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
SUPERUSER_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def writer_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(WRITER_DSN, min_size=1, max_size=4, statement_cache_size=0)
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def reader_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(READER_DSN, min_size=1, max_size=4, statement_cache_size=0)
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
    await superuser_conn.execute("TRUNCATE audit.events")
    yield


async def _seed_chain(writer: AuditWriter, tenant: UUID, n: int) -> None:
    for i in range(n):
        await writer.write_event(
            tenant_id=tenant, kind="seed", payload={"i": i}, actor_role="clinician"
        )


# ── happy-path ──────────────────────────────────────────────────────────


async def test_clean_chain_verifies(writer_pool: asyncpg.Pool, reader_pool: asyncpg.Pool) -> None:
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    verifier = AuditVerifier(reader_pool)
    await _seed_chain(writer, tenant, 25)

    report = await verifier.verify_chain(tenant)
    assert report.ok is True
    assert report.events_checked == 25
    assert report.last_seq == 25
    assert report.first_divergence_seq is None


async def test_empty_chain_verifies_vacuously(reader_pool: asyncpg.Pool) -> None:
    tenant = uuid4()
    verifier = AuditVerifier(reader_pool)
    report = await verifier.verify_chain(tenant)
    assert report.ok is True
    assert report.events_checked == 0
    assert report.last_seq is None


async def test_subrange_verifies(writer_pool: asyncpg.Pool, reader_pool: asyncpg.Pool) -> None:
    """Walking a sub-range seeds the running hash from seq-1's payload_hash."""
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    verifier = AuditVerifier(reader_pool)
    await _seed_chain(writer, tenant, 10)

    report = await verifier.verify_chain(tenant, from_seq=5, to_seq=8)
    assert report.ok is True
    assert report.events_checked == 4
    assert report.last_seq == 8


# ── adversarial: spec §1.5 ──────────────────────────────────────────────


async def test_corrupted_payload_jcs_detected(
    writer_pool: asyncpg.Pool, reader_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    """Spec adversarial #1: manually corrupt one row's payload_jcs and verify
    that the walker reports the seq of corruption."""
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    verifier = AuditVerifier(reader_pool)
    await _seed_chain(writer, tenant, 10)

    # Tamper: change payload_jcs of seq=5 (bypass trigger; only superuser can).
    await superuser_conn.execute("ALTER TABLE audit.events DISABLE TRIGGER events_no_update")
    try:
        await superuser_conn.execute(
            """
            UPDATE audit.events
            SET payload_jcs = jsonb_set(payload_jcs, '{kind}', '"tampered"')
            WHERE tenant_id = $1 AND seq = 5
            """,
            tenant,
        )
    finally:
        await superuser_conn.execute("ALTER TABLE audit.events ENABLE TRIGGER events_no_update")

    report = await verifier.verify_chain(tenant)
    assert report.ok is False
    assert report.first_divergence_seq == 5
    assert report.divergence_reason == DivergenceReason.PAYLOAD_HASH_MISMATCH
    assert report.events_checked == 4  # seq 1..4 verified before divergence


async def test_forged_payload_hash_detected(
    writer_pool: asyncpg.Pool, reader_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    """Spec adversarial #2: directly overwrite payload_hash with a new
    (matching) value. The next row's prev_hash will no longer match."""
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    verifier = AuditVerifier(reader_pool)
    await _seed_chain(writer, tenant, 10)

    await superuser_conn.execute("ALTER TABLE audit.events DISABLE TRIGGER events_no_update")
    try:
        # Replace seq=5's payload_hash with an arbitrary 32-byte value. The
        # mismatch surfaces at seq=5 itself (payload_hash_mismatch) — even
        # before the next row's prev_hash check.
        forged = b"\xff" * 32
        await superuser_conn.execute(
            "UPDATE audit.events SET payload_hash = $1 WHERE tenant_id = $2 AND seq = 5",
            forged,
            tenant,
        )
    finally:
        await superuser_conn.execute("ALTER TABLE audit.events ENABLE TRIGGER events_no_update")

    report = await verifier.verify_chain(tenant)
    assert report.ok is False
    assert report.first_divergence_seq == 5
    assert report.divergence_reason == DivergenceReason.PAYLOAD_HASH_MISMATCH


async def test_deleted_row_detected_as_gap(
    writer_pool: asyncpg.Pool, reader_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    """Spec adversarial #3: disable trigger, delete a row, re-enable.
    The walker reports a gap at the deleted seq."""
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    verifier = AuditVerifier(reader_pool)
    await _seed_chain(writer, tenant, 10)

    await superuser_conn.execute("ALTER TABLE audit.events DISABLE TRIGGER events_no_delete")
    try:
        await superuser_conn.execute(
            "DELETE FROM audit.events WHERE tenant_id = $1 AND seq = 5", tenant
        )
    finally:
        await superuser_conn.execute("ALTER TABLE audit.events ENABLE TRIGGER events_no_delete")

    report = await verifier.verify_chain(tenant)
    assert report.ok is False
    assert report.first_divergence_seq == 5
    assert report.divergence_reason == DivergenceReason.GAP


async def test_prev_hash_break_detected(
    writer_pool: asyncpg.Pool, reader_pool: asyncpg.Pool, superuser_conn: asyncpg.Connection
) -> None:
    """If an attacker rewrites a row's payload (and recomputes payload_hash
    locally) but doesn't propagate the new hash forward, the NEXT row's
    prev_hash will no longer match."""
    tenant = uuid4()
    writer = AuditWriter(writer_pool)
    verifier = AuditVerifier(reader_pool)
    await _seed_chain(writer, tenant, 10)

    await superuser_conn.execute("ALTER TABLE audit.events DISABLE TRIGGER events_no_update")
    try:
        # Replace seq=5's prev_hash with garbage. The walker at seq=5 will
        # find prev_hash != the running hash from seq=4.
        await superuser_conn.execute(
            "UPDATE audit.events SET prev_hash = $1 WHERE tenant_id = $2 AND seq = 5",
            b"\xab" * 32,
            tenant,
        )
    finally:
        await superuser_conn.execute("ALTER TABLE audit.events ENABLE TRIGGER events_no_update")

    report = await verifier.verify_chain(tenant)
    assert report.ok is False
    assert report.first_divergence_seq == 5
    assert report.divergence_reason == DivergenceReason.PREV_HASH_MISMATCH


# ── reader role is genuinely read-only ──────────────────────────────────


async def test_audit_reader_cannot_write(reader_pool: asyncpg.Pool) -> None:
    tenant = uuid4()
    async with reader_pool.acquire() as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO audit.events "
                "(tenant_id, seq, kind, payload_jcs, payload_hash) "
                "VALUES ($1, 1, 'forge', '{}'::jsonb, $2)",
                tenant,
                b"\x00" * 32,
            )
