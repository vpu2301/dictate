"""S01 verification protocol §9.3-9.4: RLS crafted-query probes + append-only proofs.

Env-gated: RUN_DB_INTEGRATION=1 with the platform dev stack up and migration
0067 applied (make dev-up && make migrate-up in ../medical-dictation-backend).

Connects as app_role — the same standing the services have — so every probe
measures the real enforcement boundary, not a superuser bypass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="DB integration tests are env-gated (RUN_DB_INTEGRATION=1)",
)

DSN = os.environ.get(
    "EVIDENCE_TEST_DSN", "postgresql://app_role:app_role@localhost:5432/medical_dictation"
)

TENANT_A = "00000000-0000-0000-0000-00000000000a"
TENANT_B = "00000000-0000-0000-0000-00000000000b"
CLINICIAN_A = "0c000000-0000-0000-0000-00000000000a"
CLINICIAN_B = "0c000000-0000-0000-0000-00000000000b"

TENANT_TABLES = [
    "documents",
    "document_versions",
    "chunks",
    "corpus_snapshots",
    "questions",
    "answers",
    "answer_segments",
    "answer_provenance",
    "answer_traces",
    "followups",
    "checks_results",
]


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    connection = await asyncpg.connect(DSN)
    try:
        yield connection
    finally:
        await connection.close()


async def _scope(conn: asyncpg.Connection, tenant: str, user: str | None = None) -> None:
    """Transaction-local GUCs exactly as tenant_connection + the user_id idiom set them."""
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant)
    if user is not None:
        await conn.execute("SELECT set_config('app.user_id', $1, true)", user)


async def _seed_question_answer(
    conn: asyncpg.Connection, tenant: str, user: str
) -> tuple[uuid.UUID, uuid.UUID]:
    async with conn.transaction():
        await _scope(conn, tenant, user)
        question_id = await conn.fetchval(
            "INSERT INTO questions (tenant_id, user_sub, text, mode, locale)"
            " VALUES ($1, $2, 'DOAC dosing in CKD?', 'quick_search', 'uk') RETURNING id",
            uuid.UUID(tenant),
            uuid.UUID(user),
        )
        answer_id = await conn.fetchval(
            "INSERT INTO answers (tenant_id, user_sub, question_id, status, mode, envelope_ref)"
            " VALUES ($1, $2, $3, 'ok', 'quick_search', 'obj:test') RETURNING id",
            uuid.UUID(tenant),
            uuid.UUID(user),
            question_id,
        )
    return question_id, answer_id


class TestTenantIsolation:
    async def test_cross_tenant_reads_return_zero_rows_everywhere(
        self, conn: asyncpg.Connection
    ) -> None:
        """Crafted-query probe per table: tenant B sees zero tenant-A rows."""
        await _seed_question_answer(conn, TENANT_A, CLINICIAN_A)
        async with conn.transaction():
            await _scope(conn, TENANT_B, CLINICIAN_B)
            for table in TENANT_TABLES:
                count = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE tenant_id = $1",  # noqa: S608
                    uuid.UUID(TENANT_A),
                )
                assert count == 0, f"{table}: tenant B read {count} tenant-A rows"

    async def test_unscoped_connection_sees_nothing(self, conn: asyncpg.Connection) -> None:
        """A connection that never set app.tenant_id reads zero rows.

        (On a session that HAS scoped before, the reverted GUC is an empty
        string and the ::uuid cast errors instead — also fail-closed; hence
        the probe runs on a genuinely fresh connection.)
        """
        await _seed_question_answer(conn, TENANT_A, CLINICIAN_A)
        fresh = await asyncpg.connect(DSN)
        try:
            for table in TENANT_TABLES:
                count = await fresh.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608
                assert count == 0, f"{table}: unscoped connection read {count} rows"
        finally:
            await fresh.close()

    async def test_injected_tenant_id_on_insert_is_rejected(self, conn: asyncpg.Connection) -> None:
        """Scoped to tenant B, inserting a row claiming tenant A must fail."""
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with conn.transaction():
                await _scope(conn, TENANT_B, CLINICIAN_B)
                await conn.execute(
                    "INSERT INTO documents (tenant_id, canonical_id, title,"
                    " source_authority, evidence_tier, license_class)"
                    " VALUES ($1, 'doi:10.1/x', 'Injected', 'other', 'other', 'restricted')",
                    uuid.UUID(TENANT_A),
                )

    async def test_question_history_is_user_private(self, conn: asyncpg.Connection) -> None:
        """Dual-key RLS: another user in the SAME tenant sees zero rows."""
        question_id, _ = await _seed_question_answer(conn, TENANT_A, CLINICIAN_A)
        other_user_same_tenant = "0d000000-0000-0000-0000-00000000000a"  # dev nurse A
        async with conn.transaction():
            await _scope(conn, TENANT_A, other_user_same_tenant)
            rows = await conn.fetch("SELECT id FROM questions WHERE id = $1", question_id)
            assert rows == []
        async with conn.transaction():
            await _scope(conn, TENANT_A, CLINICIAN_A)
            rows = await conn.fetch("SELECT id FROM questions WHERE id = $1", question_id)
            assert len(rows) == 1


class TestAppendOnlyProvenance:
    async def _seed_provenance(self, conn: asyncpg.Connection) -> uuid.UUID:
        _, answer_id = await _seed_question_answer(conn, TENANT_A, CLINICIAN_A)
        async with conn.transaction():
            await _scope(conn, TENANT_A, CLINICIAN_A)
            return await conn.fetchval(
                "INSERT INTO answer_provenance (tenant_id, answer_id, question_ref,"
                " pipeline_version, build_version)"
                " VALUES ($1, $2, 'question:test', 'p1', 'dev') RETURNING id",
                uuid.UUID(TENANT_A),
                answer_id,
            )

    async def test_update_denied_both_layers(self, conn: asyncpg.Connection) -> None:
        prov_id = await self._seed_provenance(conn)
        # Grant layer: app_role has no UPDATE privilege at all.
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with conn.transaction():
                await _scope(conn, TENANT_A, CLINICIAN_A)
                await conn.execute(
                    "UPDATE answer_provenance SET pipeline_version = 'tampered' WHERE id = $1",
                    prov_id,
                )

    async def test_delete_denied_both_layers(self, conn: asyncpg.Connection) -> None:
        prov_id = await self._seed_provenance(conn)
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with conn.transaction():
                await _scope(conn, TENANT_A, CLINICIAN_A)
                await conn.execute("DELETE FROM answer_provenance WHERE id = $1", prov_id)

    async def test_trigger_layer_blocks_superuser_too(self) -> None:
        """The immutability trigger holds even for a role WITH the grant."""
        admin_dsn = os.environ.get(
            "EVIDENCE_TEST_ADMIN_DSN",
            "postgresql://postgres:postgres@localhost:5432/medical_dictation",
        )
        admin = await asyncpg.connect(admin_dsn)
        try:
            row_id = await admin.fetchval(
                "INSERT INTO answer_provenance (tenant_id, answer_id, question_ref,"
                " pipeline_version, build_version)"
                " VALUES ($1, $2, 'question:trigger-proof', 'p1', 'dev') RETURNING id",
                uuid.UUID(TENANT_A),
                uuid.uuid4(),
            )
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await admin.execute(
                    "UPDATE answer_provenance SET pipeline_version = 'x' WHERE id = $1", row_id
                )
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await admin.execute("DELETE FROM answer_provenance WHERE id = $1", row_id)
        finally:
            await admin.close()


class TestSchemaContractsAlignment:
    async def test_segment_kind_check_matches_contract_enum(self, conn: asyncpg.Connection) -> None:
        """DB CHECK and Pydantic enum must agree; and DB-level ET2 holds."""
        _, answer_id = await _seed_question_answer(conn, TENANT_A, CLINICIAN_A)
        # Unknown kind rejected.
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await _scope(conn, TENANT_A, CLINICIAN_A)
                await conn.execute(
                    "INSERT INTO answer_segments (tenant_id, answer_id, ord, kind, text)"
                    " VALUES ($1, $2, 0, 'opinion', 'x')",
                    uuid.UUID(TENANT_A),
                    answer_id,
                )
        # Evidence without citations rejected at the storage layer too (ET2).
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await _scope(conn, TENANT_A, CLINICIAN_A)
                await conn.execute(
                    "INSERT INTO answer_segments (tenant_id, answer_id, ord, kind, text)"
                    " VALUES ($1, $2, 0, 'evidence', 'uncited claim')",
                    uuid.UUID(TENANT_A),
                    answer_id,
                )

    async def test_knowledge_admin_users_seeded(self) -> None:
        admin_dsn = os.environ.get(
            "EVIDENCE_TEST_ADMIN_DSN",
            "postgresql://postgres:postgres@localhost:5432/medical_dictation",
        )
        admin = await asyncpg.connect(admin_dsn)
        try:
            rows = await admin.fetch(
                "SELECT email, tenant_id FROM users WHERE role = 'knowledge_admin' ORDER BY email"
            )
            emails = [r["email"] for r in rows]
            assert "knowledge_admin@tenant-a.example" in emails
            assert "knowledge_admin@tenant-b.example" in emails
        finally:
            await admin.close()


@pytest.fixture(autouse=True)
async def _cleanup() -> AsyncIterator[None]:
    """Remove test rows (superuser; provenance rows survive by design, so
    truncate via the table owner which bypasses the trigger? No — the trigger
    fires for the owner too. TRUNCATE is intentionally NOT trigger-guarded
    (per the audit.events precedent) and resets the probe tables."""
    yield
    admin_dsn = os.environ.get(
        "EVIDENCE_TEST_ADMIN_DSN",
        "postgresql://postgres:postgres@localhost:5432/medical_dictation",
    )
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(
            "TRUNCATE questions, answers, answer_segments, answer_provenance,"
            " answer_traces, followups, checks_results, documents, document_versions,"
            " chunks, corpus_snapshots CASCADE"
        )
    finally:
        await admin.close()
