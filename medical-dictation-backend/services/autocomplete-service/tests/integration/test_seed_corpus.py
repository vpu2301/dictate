"""Sprint-10 step-02 integration — migration 0026 seed behaviour.

Proves against the real DB:
- seeded row counts match the committed CSV/JSON corpus files;
- re-applying 0026 is idempotent (ON CONFLICT DO NOTHING);
- 0026's down removes EXACTLY the starter rows — a non-starter system
  row (a stand-in for a future 10k corpus drop) survives;
- a seeded phrase is SELECTable from a tenant connection (step-01
  PERMISSIVE policy proven against real seed data).

NOTE: the down/up cycle re-creates starter rows with fresh UUIDs and
zeroed counters — fine in dev, where counters are demo data.

Skipped unless ``RUN_DB_INTEGRATION=1``.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from uuid import UUID

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
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")

REPO = Path(__file__).resolve().parents[4]
SEED_DIR = REPO / "infra" / "seeds" / "autocomplete"
MIG_DIR = REPO / "infra" / "postgres" / "migrations"
UP_SQL = (MIG_DIR / "0026_seed_autocomplete_system_corpus.sql").read_text("utf-8")
DOWN_SQL = (
    MIG_DIR / "0026_seed_autocomplete_system_corpus.down.sql"
).read_text("utf-8")

EXTRA_PHRASE = "itest-0026 майбутній корпус не чіпати"


def _committed_counts() -> tuple[int, int, list[str]]:
    phrases: list[str] = []
    for f in sorted(SEED_DIR.glob("phrases_*.csv")):
        with open(f, encoding="utf-8") as fh:
            phrases.extend(r["phrase"] for r in csv.DictReader(fh))
    n_snippets = sum(
        len(json.loads(f.read_text("utf-8")))
        for f in sorted(SEED_DIR.glob("snippets_*.json"))
    )
    return len(phrases), n_snippets, phrases


async def test_seed_matches_corpus_idempotent_and_scoped_down():
    n_phrases, n_snippets, phrases = _committed_counts()
    su = await asyncpg.connect(SU_DSN)
    try:
        # Exclude corpus-promoted provenance: since sprint 21 the promote job
        # legitimately adds system-scope rows (mined/terminology/generated,
        # ADR-0043). 0026 rows are 'seed' after the 0081 backfill but come
        # back as the column default 'authored' when this test re-applies the
        # (immutable, checksummed) seed SQL — count both.
        counts = """
            SELECT (SELECT count(*) FROM autocomplete_phrases
                    WHERE source='system' AND source_kind IN ('seed', 'authored')),
                   (SELECT count(*) FROM autocomplete_snippets WHERE source='system')
        """
        p0, s0 = await su.fetchrow(counts)
        assert p0 == n_phrases, f"DB has {p0} system phrases, corpus has {n_phrases}"
        assert s0 == n_snippets

        # Idempotent re-apply.
        await su.execute(UP_SQL)
        p1, s1 = await su.fetchrow(counts)
        assert (p1, s1) == (p0, s0), "re-applying 0026 changed row counts"

        # Scoped down: a non-starter system row must survive.
        await su.execute(
            "INSERT INTO autocomplete_phrases (phrase, language, specialty, source) "
            "VALUES ($1, 'uk', 'general', 'system')",
            EXTRA_PHRASE,
        )
        try:
            await su.execute(DOWN_SQL)
            p2, s2 = await su.fetchrow(counts)
            assert (p2, s2) == (1, 0), (
                f"down left {p2} phrases / {s2} snippets; expected exactly "
                "the 1 non-starter row"
            )
            survivor = await su.fetchval(
                "SELECT phrase FROM autocomplete_phrases "
                "WHERE source='system' AND source_kind IN ('seed', 'authored')"
            )
            assert survivor == EXTRA_PHRASE
        finally:
            # Restore the starter corpus and drop the probe row.
            await su.execute(UP_SQL)
            await su.execute(
                "DELETE FROM autocomplete_phrases WHERE phrase = $1", EXTRA_PHRASE
            )
        p3, s3 = await su.fetchrow(counts)
        assert (p3, s3) == (p0, s0), "restore did not return to the seeded state"
    finally:
        await su.close()

    # System visibility via RLS with real seed data (step-01 §6.1 on 0026 rows).
    pool = await create_pool(APP_DSN, application_name="seed-itest", min_size=1, max_size=1)
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM autocomplete_phrases "
                "WHERE source='system' AND phrase = ANY($1::text[])",
                phrases,
            )
        assert n == n_phrases, "tenant connection cannot see all seeded system phrases"
    finally:
        await pool.close()
