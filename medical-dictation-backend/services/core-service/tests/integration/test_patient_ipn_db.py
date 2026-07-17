"""S11 step 01 §8 integration — migration 0042 semantics against a live DB.

Proves at the database layer:

- the partial unique index allows the same ``ipn_hmac`` in different
  tenants, forbids it within a tenant, and frees the slot once the
  holder is ``erased``;
- the ``(ipn_encrypted IS NULL) = (ipn_dek IS NULL)`` pair CHECK;
- the ``(status='erased') = (erased_at IS NOT NULL)`` CHECK.

Skipped unless ``RUN_DB_INTEGRATION=1`` (needs ``make dev-up && make
migrate-up && make seed``). All ІПН hmacs are random bytes — no real
identifiers are involved.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs `make dev-up && make migrate-up && make seed`",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"

MARK = "itest-ipn-0042"


async def _two_tenants_with_users(su: asyncpg.Connection) -> list[tuple[UUID, UUID]]:
    rows = await su.fetch(
        """
        SELECT DISTINCT ON (t.id) t.id AS tenant_id, u.sub AS user_sub
        FROM tenants t JOIN users u ON u.tenant_id = t.id
        ORDER BY t.id LIMIT 2
        """
    )
    if len(rows) < 2:
        pytest.skip("needs two seeded tenants with users (`make seed`)")
    return [(r["tenant_id"], r["user_sub"]) for r in rows]


async def _insert_patient(
    su: asyncpg.Connection,
    *,
    tenant_id: UUID,
    created_by: UUID,
    ipn_hmac: bytes | None,
    status: str = "active",
    erased_at: datetime | None = None,
    ipn_encrypted: bytes | None = None,
    ipn_dek: bytes | None = None,
) -> UUID:
    return await su.fetchval(
        """
        INSERT INTO patients
            (tenant_id, name_uk, mrn, created_by, ipn_hmac,
             status, erased_at, ipn_encrypted, ipn_dek)
        VALUES ($1, $2, '', $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        tenant_id,
        MARK,
        created_by,
        ipn_hmac,
        status,
        erased_at,
        ipn_encrypted,
        ipn_dek,
    )


async def _cleanup(su: asyncpg.Connection) -> None:
    await su.execute("DELETE FROM patients WHERE name_uk = $1", MARK)


async def test_partial_unique_index_semantics() -> None:
    su = await asyncpg.connect(SU_DSN)
    try:
        await _cleanup(su)
        (tenant_a, user_a), (tenant_b, user_b) = await _two_tenants_with_users(su)
        hmac = os.urandom(32)

        first = await _insert_patient(
            su, tenant_id=tenant_a, created_by=user_a, ipn_hmac=hmac
        )

        # Same hmac, same tenant → refused.
        with pytest.raises(asyncpg.UniqueViolationError) as exc_info:
            await _insert_patient(
                su, tenant_id=tenant_a, created_by=user_a, ipn_hmac=hmac
            )
        assert exc_info.value.constraint_name == "uq_patients_tenant_ipn"

        # Same hmac, other tenant → fine (index is per-tenant).
        await _insert_patient(su, tenant_id=tenant_b, created_by=user_b, ipn_hmac=hmac)

        # Erase the holder → the slot frees up for re-registration.
        await su.execute(
            "UPDATE patients SET status = 'erased', erased_at = now() WHERE id = $1",
            first,
        )
        await _insert_patient(su, tenant_id=tenant_a, created_by=user_a, ipn_hmac=hmac)

        # NULL hmacs never collide.
        await _insert_patient(su, tenant_id=tenant_a, created_by=user_a, ipn_hmac=None)
        await _insert_patient(su, tenant_id=tenant_a, created_by=user_a, ipn_hmac=None)
    finally:
        await _cleanup(su)
        await su.close()


async def test_ipn_pair_check_violation() -> None:
    su = await asyncpg.connect(SU_DSN)
    try:
        await _cleanup(su)
        (tenant_a, user_a), _ = await _two_tenants_with_users(su)

        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_patient(
                su,
                tenant_id=tenant_a,
                created_by=user_a,
                ipn_hmac=os.urandom(32),
                ipn_encrypted=b"ciphertext-without-dek",
                ipn_dek=None,
            )
        # Both present is fine.
        await _insert_patient(
            su,
            tenant_id=tenant_a,
            created_by=user_a,
            ipn_hmac=os.urandom(32),
            ipn_encrypted=b"ciphertext",
            ipn_dek=b"wrapped-dek",
        )
    finally:
        await _cleanup(su)
        await su.close()


async def test_erased_requires_timestamp_check() -> None:
    su = await asyncpg.connect(SU_DSN)
    try:
        await _cleanup(su)
        (tenant_a, user_a), _ = await _two_tenants_with_users(su)

        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_patient(
                su,
                tenant_id=tenant_a,
                created_by=user_a,
                ipn_hmac=None,
                status="erased",
                erased_at=None,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_patient(
                su,
                tenant_id=tenant_a,
                created_by=user_a,
                ipn_hmac=None,
                status="active",
                erased_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
    finally:
        await _cleanup(su)
        await su.close()
