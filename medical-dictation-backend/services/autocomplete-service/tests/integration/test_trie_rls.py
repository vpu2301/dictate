"""Step-03 §8 integration — RLS through the builder.

The builder never filters scopes itself: ``fetch_corpus`` runs under a
tenant connection and the DATABASE enforces visibility. Prove that a
trie built for tenant A contains system + tenant-A rows and never
tenant B's.

Skipped unless ``RUN_DB_INTEGRATION=1``.
"""

from __future__ import annotations

import os
from uuid import UUID

import asyncpg
import pytest
from autocomplete_service import repository as repo
from autocomplete_service.trie.builder import build_trie_from_phrases

from db import create_pool, tenant_connection

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs `make dev-up && make migrate-up`",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")

MARK_A = "itest-trie тенант а фраза"
MARK_B = "itest-trie тенант б секрет"


async def test_builder_sees_own_scopes_never_other_tenants():
    su = await asyncpg.connect(SU_DSN)
    try:
        await su.execute(
            "INSERT INTO autocomplete_phrases (tenant_id, phrase, language, source) "
            "VALUES ($1, $2, 'uk', 'tenant'), ($3, $4, 'uk', 'tenant')",
            TENANT_A, MARK_A, TENANT_B, MARK_B,
        )
        pool = await create_pool(
            APP_DSN, application_name="trie-rls-itest", min_size=1, max_size=1
        )
        try:
            async with tenant_connection(pool, TENANT_A) as conn:
                rows = await repo.fetch_corpus(conn, language="uk")
        finally:
            await pool.close()

        trie = build_trie_from_phrases(
            tenant_id=str(TENANT_A), language="uk", user_id="itest", rows=rows
        )
        phrases = {e.phrase for e in trie.entries.values()}
        assert MARK_A in phrases, "own-tenant row missing from the trie"
        assert MARK_B not in phrases, "ANOTHER TENANT'S row leaked into the trie"
        # System seed rows are visible to every tenant.
        assert any(p == "задишка при фізичному навантаженні" for p in phrases)
        # And the trie serves them.
        assert trie.candidates_for("itest-trie тенант")
    finally:
        await su.execute(
            "DELETE FROM autocomplete_phrases WHERE phrase IN ($1, $2)", MARK_A, MARK_B
        )
        await su.close()
