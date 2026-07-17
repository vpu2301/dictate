"""S11 step 02 §8 integration — `audio_files.encounter_id` FK semantics.

Proves at the database layer:

- the FK rejects a random (nonexistent) encounter UUID;
- ON DELETE RESTRICT blocks deleting an encounter that has recordings;
- NULL `encounter_id` rows are unaffected;
- the 0043 orphan guard raises on a planted orphan (constraint dropped
  inside a rolled-back transaction so the schema is never left dirty).

Skipped unless ``RUN_DB_INTEGRATION=1`` (needs ``make dev-up && make
migrate-up && make seed``).
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

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

MARK = "itest-encounter-fk-0043"

ORPHAN_GUARD_SQL = """
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM audio_files a
             WHERE a.encounter_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM encounters e
                               WHERE e.id = a.encounter_id))
  THEN RAISE EXCEPTION 'orphan audio_files.encounter_id rows';
  END IF;
END $$;
"""


async def _fixture_ids(su: asyncpg.Connection) -> tuple[UUID, UUID, UUID]:
    """(tenant_id, user_sub, encounter_id) with a fresh patient+encounter."""
    row = await su.fetchrow(
        "SELECT t.id AS tenant_id, u.sub FROM tenants t "
        "JOIN users u ON u.tenant_id = t.id LIMIT 1"
    )
    if row is None:
        pytest.skip("needs a seeded tenant with a user (`make seed`)")
    patient_id = await su.fetchval(
        "INSERT INTO patients (tenant_id, name_uk, created_by) "
        "VALUES ($1, $2, $3) RETURNING id",
        row["tenant_id"],
        MARK,
        row["sub"],
    )
    encounter_id = await su.fetchval(
        "INSERT INTO encounters (tenant_id, patient_id, created_by, reason) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        row["tenant_id"],
        patient_id,
        row["sub"],
        MARK,
    )
    return row["tenant_id"], row["sub"], encounter_id


async def _insert_audio(
    su: asyncpg.Connection,
    *,
    tenant_id: UUID,
    uploader: UUID,
    encounter_id: UUID | None,
) -> UUID:
    return await su.fetchval(
        """
        INSERT INTO audio_files
            (tenant_id, uploader_sub, mime_type, size_bytes, duration_ms,
             sha256, envelope_metadata, storage_uri, status, encounter_id)
        VALUES ($1, $2, 'audio/wav', 1, 1, $3, '{}'::jsonb, $4, 'stored', $5)
        RETURNING id
        """,
        tenant_id,
        uploader,
        b"\x00" * 32,
        f"minio://itest/{MARK}",
        encounter_id,
    )


async def _cleanup(su: asyncpg.Connection) -> None:
    await su.execute("DELETE FROM audio_files WHERE storage_uri = $1", f"minio://itest/{MARK}")
    await su.execute(
        "DELETE FROM encounters WHERE reason = $1", MARK
    )
    await su.execute("DELETE FROM patients WHERE name_uk = $1", MARK)


async def test_fk_rejects_nonexistent_encounter() -> None:
    su = await asyncpg.connect(SU_DSN)
    try:
        await _cleanup(su)
        tenant_id, uploader, _ = await _fixture_ids(su)
        with pytest.raises(asyncpg.ForeignKeyViolationError) as exc_info:
            await _insert_audio(
                su, tenant_id=tenant_id, uploader=uploader, encounter_id=uuid4()
            )
        assert exc_info.value.constraint_name == "audio_files_encounter_fk"
    finally:
        await _cleanup(su)
        await su.close()


async def test_restrict_blocks_encounter_delete_and_null_is_fine() -> None:
    su = await asyncpg.connect(SU_DSN)
    try:
        await _cleanup(su)
        tenant_id, uploader, encounter_id = await _fixture_ids(su)

        # NULL encounter_id stays legal (ad-hoc recording).
        await _insert_audio(su, tenant_id=tenant_id, uploader=uploader, encounter_id=None)

        # Linked recording → deleting the encounter is RESTRICTed.
        await _insert_audio(
            su, tenant_id=tenant_id, uploader=uploader, encounter_id=encounter_id
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await su.execute("DELETE FROM encounters WHERE id = $1", encounter_id)

        # Unlink → delete proceeds (the erasure engine's ordering).
        await su.execute(
            "DELETE FROM audio_files WHERE encounter_id = $1", encounter_id
        )
        await su.execute("DELETE FROM encounters WHERE id = $1", encounter_id)
    finally:
        await _cleanup(su)
        await su.close()


async def test_orphan_guard_raises_on_planted_orphan() -> None:
    """Re-run the 0043 guard against a planted orphan — inside a
    transaction that drops the FK and rolls everything back."""
    su = await asyncpg.connect(SU_DSN)
    try:
        await _cleanup(su)
        tenant_id, uploader, _ = await _fixture_ids(su)
        tx = su.transaction()
        await tx.start()
        try:
            await su.execute(
                "ALTER TABLE audio_files DROP CONSTRAINT audio_files_encounter_fk"
            )
            await _insert_audio(
                su, tenant_id=tenant_id, uploader=uploader, encounter_id=uuid4()
            )
            with pytest.raises(asyncpg.RaiseError, match="orphan audio_files"):
                await su.execute(ORPHAN_GUARD_SQL)
        finally:
            await tx.rollback()

        # Sanity: the FK survived the rollback.
        assert await su.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname = 'audio_files_encounter_fk'"
        ) == 1
    finally:
        await _cleanup(su)
        await su.close()
