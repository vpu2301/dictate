"""ICD-10 search ranking + latency (sprint 13, step 03).

Exercises the SHARED ranking SQL (``db.ICD10_SEARCH_SQL``) directly
against the loaded table — the same statement the HTTP endpoint and
the nlp extractor both run.

Skipped unless ``RUN_DB_INTEGRATION=1`` with the dev stack up,
migrated, and the fixture loaded (``make seed-icd10-fixture``).
"""

from __future__ import annotations

import os
import statistics
import time

import asyncpg
import pytest

from db import ICD10_SEARCH_SQL, create_pool

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1; needs dev-up + migrate-up + make seed-icd10-fixture",
)

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"

# p95 budget for the picker's typing path (step 03 §4.3).
LATENCY_BUDGET_MS = 50.0


@pytest.fixture
async def pool() -> asyncpg.Pool:
    p = await create_pool(APP_DSN, application_name="icd10-search-test")
    try:
        yield p
    finally:
        await p.close()


async def _search(pool: asyncpg.Pool, q: str, limit: int = 10) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(ICD10_SEARCH_SQL, q, limit)


async def test_exact_code_match_ranks_first(pool: asyncpg.Pool) -> None:
    rows = await _search(pool, "I10")
    assert rows[0]["code"] == "I10"


async def test_exact_code_match_is_case_insensitive(pool: asyncpg.Pool) -> None:
    rows = await _search(pool, "i10")
    assert rows[0]["code"] == "I10"


async def test_code_prefix_surfaces_the_family(pool: asyncpg.Pool) -> None:
    codes = [r["code"] for r in await _search(pool, "E11", limit=20)]
    assert codes[0] == "E11"
    assert {"E11.9", "E11.2"} <= set(codes)


async def test_uk_fulltext_finds_the_hypertension_family(pool: asyncpg.Pool) -> None:
    codes = [r["code"] for r in await _search(pool, "гіпертонічна", limit=20)]
    assert any(c.startswith("I1") for c in codes), codes


async def test_uk_fulltext_finds_diabetes(pool: asyncpg.Pool) -> None:
    codes = [r["code"] for r in await _search(pool, "цукровий діабет", limit=20)]
    assert any(c.startswith("E11") for c in codes), codes


async def test_leaves_rank_before_headings_within_a_tier(pool: asyncpg.Pool) -> None:
    rows = await _search(pool, "астма", limit=20)
    leaf_flags = [r["is_leaf"] for r in rows]
    # Once a heading appears, no leaf may follow it at the same tier.
    tiers = [r["tier"] for r in rows]
    for tier in set(tiers):
        flags = [f for f, t in zip(leaf_flags, tiers, strict=True) if t == tier]
        assert flags == sorted(flags, reverse=True), rows


async def test_partial_word_matches_by_prefix(pool: asyncpg.Pool) -> None:
    # THE search-as-you-type case: nearly every keystroke is a partial word.
    # plainto_tsquery's whole-word matching returned nothing here (found
    # live 2026-07-23 — the picker looked dead while typing «гіперт…»);
    # the query must prefix-match each lexeme.
    hyper = {r["code"] for r in await _search(pool, "гіперт")}
    assert "I10" in hyper, hyper
    sten = {r["code"] for r in await _search(pool, "стенокард")}
    assert {"I20.0", "I20.9"} <= sten, sten


async def test_multi_word_partial_still_conjunctive(pool: asyncpg.Pool) -> None:
    # All words must match (AND), each by prefix — «гіпертонічна хвор»
    # narrows to the hypertensive-disease family, not everything matching
    # either word.
    rows = await _search(pool, "гіпертонічна хвор")
    assert rows, "conjunctive prefix query must match"
    assert all("Гіпертонічна хвороба" in r["display_uk"] for r in rows), rows


async def test_apostrophe_in_query_does_not_error(pool: asyncpg.Pool) -> None:
    # Ukrainian medical vocabulary is full of apostrophes («м'язів»);
    # lexemes are re-quoted before to_tsquery, so this must not raise.
    await _search(pool, "хвороба м'язів")


async def test_limit_is_respected(pool: asyncpg.Pool) -> None:
    assert len(await _search(pool, "а", limit=3)) <= 3


async def test_no_match_returns_empty(pool: asyncpg.Pool) -> None:
    assert await _search(pool, "zzzznotarealterm") == []


async def test_punctuation_only_query_does_not_error(pool: asyncpg.Pool) -> None:
    # plainto_tsquery yields an empty query here; must not raise.
    assert await _search(pool, "...") == []


async def test_search_p95_within_budget(pool: asyncpg.Pool) -> None:
    queries = ["I10", "E11", "гіпертонічна", "діабет", "астма", "біль", "J44", "пневмонія"]
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM icd10_codes")
        await conn.fetch(ICD10_SEARCH_SQL, "warmup", 10)  # prime plan + cache
        samples: list[float] = []
        for i in range(200):
            q = queries[i % len(queries)]
            started = time.perf_counter()
            await conn.fetch(ICD10_SEARCH_SQL, q, 10)
            samples.append((time.perf_counter() - started) * 1000)

    samples.sort()
    p50 = statistics.median(samples)
    p95 = samples[int(len(samples) * 0.95) - 1]
    print(
        f"\nicd10 search over {total} codes, {len(samples)} iterations: "
        f"p50={p50:.2f} ms p95={p95:.2f} ms max={samples[-1]:.2f} ms"
    )
    assert p95 <= LATENCY_BUDGET_MS, f"p95 {p95:.2f} ms exceeds {LATENCY_BUDGET_MS} ms"
