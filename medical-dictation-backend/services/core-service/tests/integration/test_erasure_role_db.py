"""S11 step 04 §8 integration — the privilege boundary, proven on a live DB.

The headline checks:

- ``app_role`` still cannot DELETE ``audio_files`` / ``reports`` rows
  (the sprint-03 reservation holds — regression guard);
- ``mdx_erasure`` CAN delete, tenant-scoped: in-tenant DELETE removes the
  row, a wrong-tenant GUC deletes 0 rows (its RLS policies proven);
- ``mdx_erasure`` can UPDATE ``patients`` (identity overwrite) but has no
  DELETE grant on it (the tombstone survives);
- the two-person + approved-has-review CHECKs reject bad rows;
- ``mark_executing`` refuses before ``scheduled_for`` (grace at the data
  layer).

Skipped unless ``RUN_DB_INTEGRATION=1``. Needs migrate-up ≥ 0044 and the
``mdx_erasure`` role (fresh volumes: ``make reset-db``; existing dev
volumes: run the init.sql role block once).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg
import pytest

from db import create_pool, tenant_connection

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs `make dev-up && make migrate-up && make seed`",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
ERASURE_DSN = (
    f"postgresql://mdx_erasure:mdx_erasure@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
)

MARK = "itest-erasure-0044"


async def _tenant_and_user(su: asyncpg.Connection) -> tuple[UUID, UUID, UUID]:
    """(tenant_a, tenant_b, user_a_sub) from the seeds."""
    rows = await su.fetch(
        "SELECT DISTINCT ON (t.id) t.id AS tid, u.sub FROM tenants t "
        "JOIN users u ON u.tenant_id = t.id ORDER BY t.id LIMIT 2"
    )
    if len(rows) < 2:
        pytest.skip("needs two seeded tenants with users (`make seed`)")
    return rows[0]["tid"], rows[1]["tid"], rows[0]["sub"]


async def _plant_audio(su: asyncpg.Connection, tenant_id: UUID, uploader: UUID) -> UUID:
    return await su.fetchval(
        """
        INSERT INTO audio_files
            (tenant_id, uploader_sub, mime_type, size_bytes, duration_ms,
             sha256, envelope_metadata, storage_uri, status)
        VALUES ($1, $2, 'audio/wav', 1, 1, $3, '{}'::jsonb, $4, 'stored')
        RETURNING id
        """,
        tenant_id,
        uploader,
        b"\x00" * 32,
        f"minio://itest/{MARK}",
    )


async def _cleanup(su: asyncpg.Connection) -> None:
    await su.execute("DELETE FROM audio_files WHERE storage_uri = $1", f"minio://itest/{MARK}")
    await su.execute("DELETE FROM patient_privacy_requests WHERE reason = $1", MARK)
    await su.execute("DELETE FROM patients WHERE name_uk = $1", MARK)


async def test_app_role_still_cannot_delete_phi() -> None:
    su = await asyncpg.connect(SU_DSN)
    app_pool = await create_pool(APP_DSN, application_name="itest", min_size=1, max_size=1)
    try:
        await _cleanup(su)
        tenant_a, _, user_a = await _tenant_and_user(su)
        audio_id = await _plant_audio(su, tenant_a, user_a)

        async with tenant_connection(app_pool, tenant_a) as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("DELETE FROM audio_files WHERE id = $1", audio_id)
        async with tenant_connection(app_pool, tenant_a) as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("DELETE FROM reports WHERE false")
    finally:
        await _cleanup(su)
        await app_pool.close()
        await su.close()


async def test_mdx_erasure_deletes_in_tenant_only() -> None:
    su = await asyncpg.connect(SU_DSN)
    erasure_pool = await create_pool(
        ERASURE_DSN, application_name="itest-erasure", min_size=1, max_size=1
    )
    try:
        await _cleanup(su)
        tenant_a, tenant_b, user_a = await _tenant_and_user(su)
        audio_id = await _plant_audio(su, tenant_a, user_a)

        # Wrong tenant GUC → RLS hides the row → 0 deleted.
        async with tenant_connection(erasure_pool, tenant_b) as conn:
            result = await conn.execute("DELETE FROM audio_files WHERE id = $1", audio_id)
        assert result == "DELETE 0"
        assert await su.fetchval(
            "SELECT count(*) FROM audio_files WHERE id = $1", audio_id
        ) == 1

        # Correct tenant → the reserved DELETE finally works.
        async with tenant_connection(erasure_pool, tenant_a) as conn:
            result = await conn.execute("DELETE FROM audio_files WHERE id = $1", audio_id)
        assert result == "DELETE 1"
        assert await su.fetchval(
            "SELECT count(*) FROM audio_files WHERE id = $1", audio_id
        ) == 0
    finally:
        await _cleanup(su)
        await erasure_pool.close()
        await su.close()


async def test_mdx_erasure_updates_patients_but_cannot_delete_them() -> None:
    su = await asyncpg.connect(SU_DSN)
    erasure_pool = await create_pool(
        ERASURE_DSN, application_name="itest-erasure", min_size=1, max_size=1
    )
    try:
        await _cleanup(su)
        tenant_a, _, user_a = await _tenant_and_user(su)
        patient_id = await su.fetchval(
            "INSERT INTO patients (tenant_id, name_uk, created_by) "
            "VALUES ($1, $2, $3) RETURNING id",
            tenant_a,
            MARK,
            user_a,
        )
        # Identity overwrite works (the step-07 engine's tombstone write).
        async with tenant_connection(erasure_pool, tenant_a) as conn:
            result = await conn.execute(
                "UPDATE patients SET status = 'erased', erased_at = now(), "
                "ipn_hmac = NULL WHERE id = $1",
                patient_id,
            )
        assert result == "UPDATE 1"
        # The tombstone row can never be deleted by anyone but a superuser.
        async with tenant_connection(erasure_pool, tenant_a) as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("DELETE FROM patients WHERE id = $1", patient_id)
    finally:
        await _cleanup(su)
        await erasure_pool.close()
        await su.close()


async def test_two_person_and_review_checks() -> None:
    su = await asyncpg.connect(SU_DSN)
    try:
        await _cleanup(su)
        tenant_a, _, user_a = await _tenant_and_user(su)
        patient_id = await su.fetchval(
            "INSERT INTO patients (tenant_id, name_uk, created_by) "
            "VALUES ($1, $2, $3) RETURNING id",
            tenant_a,
            MARK,
            user_a,
        )

        # Self-approval violates privacy_two_person.
        with pytest.raises(asyncpg.CheckViolationError) as exc_info:
            await su.execute(
                """
                INSERT INTO patient_privacy_requests
                    (tenant_id, patient_id, kind, reason, status,
                     requested_by, reviewed_by, reviewed_at, scheduled_for)
                VALUES ($1, $2, 'erasure', $3, 'approved', $4, $4, now(), now())
                """,
                tenant_a,
                patient_id,
                MARK,
                user_a,
            )
        assert exc_info.value.constraint_name == "privacy_two_person"

        # Approved without a reviewer violates privacy_approved_has_review.
        with pytest.raises(asyncpg.CheckViolationError) as exc_info:
            await su.execute(
                """
                INSERT INTO patient_privacy_requests
                    (tenant_id, patient_id, kind, reason, status, requested_by)
                VALUES ($1, $2, 'erasure', $3, 'approved', $4)
                """,
                tenant_a,
                patient_id,
                MARK,
                user_a,
            )
        assert exc_info.value.constraint_name == "privacy_approved_has_review"
    finally:
        await _cleanup(su)
        await su.close()


async def test_grace_period_enforced_at_data_layer() -> None:
    from core_service.domain import privacy_repository

    su = await asyncpg.connect(SU_DSN)
    app_pool = await create_pool(APP_DSN, application_name="itest", min_size=1, max_size=1)
    try:
        await _cleanup(su)
        tenant_a, tenant_b, user_a = await _tenant_and_user(su)
        user_b_sub = await su.fetchval(
            "SELECT sub FROM users WHERE tenant_id = $1 AND sub <> $2 LIMIT 1", tenant_a, user_a
        )
        if user_b_sub is None:
            pytest.skip("needs two users in tenant A")
        patient_id = await su.fetchval(
            "INSERT INTO patients (tenant_id, name_uk, created_by) "
            "VALUES ($1, $2, $3) RETURNING id",
            tenant_a,
            MARK,
            user_a,
        )
        request_id = await su.fetchval(
            """
            INSERT INTO patient_privacy_requests
                (tenant_id, patient_id, kind, reason, status,
                 requested_by, reviewed_by, reviewed_at, scheduled_for)
            VALUES ($1, $2, 'erasure', $3, 'approved', $4, $5, now(), $6)
            RETURNING id
            """,
            tenant_a,
            patient_id,
            MARK,
            user_a,
            user_b_sub,
            datetime.now(UTC) + timedelta(days=7),
        )

        # Grace still running → the engine transition is refused.
        async with tenant_connection(app_pool, tenant_a) as conn:
            row = await privacy_repository.mark_executing(conn, request_id=request_id)
        assert row is None

        # Grace elapsed → allowed.
        await su.execute(
            "UPDATE patient_privacy_requests SET scheduled_for = now() - interval '1 hour' "
            "WHERE id = $1",
            request_id,
        )
        async with tenant_connection(app_pool, tenant_a) as conn:
            row = await privacy_repository.mark_executing(conn, request_id=request_id)
        assert row is not None and row["status"] == "executing"
    finally:
        await _cleanup(su)
        await app_pool.close()
        await su.close()
