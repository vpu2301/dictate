"""CI gate — the fan-out map cannot silently drift (S11 step 05).

Computes the FK closure from ``patients(id)`` (depth 3) on the live
schema and fails when:

1. a closure table is neither registered in ``core_service.erasure.
   fanout.FANOUT`` nor justified in ``KNOWN_NON_PHI`` (a new
   patient-linked table was added without deciding its DSAR/erasure
   fate);
2. a FANOUT table no longer exists (dead map entry);
3. a ``SOFT_LINKED_PHI`` table (resource_id linkage, invisible to FK
   scanning — signed_envelopes, signing_sessions) is missing from
   FANOUT — exactly the class of edge this scan cannot see, asserted
   by name.

Run via ``make check-erasure-fanout`` (wired into ``ci-with-db``):
    uv run --project services/core-service python scripts/ci/check_erasure_fanout_coverage.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

from core_service.erasure.fanout import FANOUT, KNOWN_NON_PHI, SOFT_LINKED_PHI

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/medical_dictation"

CLOSURE_SQL = """
WITH RECURSIVE fk AS (
    SELECT conrelid::regclass::text AS child, confrelid::regclass::text AS parent
    FROM pg_constraint
    WHERE contype = 'f' AND connamespace = 'public'::regnamespace
), closure AS (
    SELECT child, parent, 1 AS depth FROM fk WHERE parent = 'patients'
    UNION
    SELECT f.child, f.parent, c.depth + 1
    FROM fk f JOIN closure c ON f.parent = c.child
    WHERE c.depth < 3
)
SELECT DISTINCT child FROM closure
"""

EXISTS_SQL = """
SELECT count(*) FROM information_schema.tables
WHERE table_schema = 'public' AND table_name = $1
"""


def run_checks(
    *,
    closure_tables: set[str],
    existing_map_tables: set[str],
    fanout_tables: set[str],
    allowlist: set[str],
    soft_linked: set[str],
) -> list[str]:
    """Pure check logic (unit-tested; the async main feeds it)."""
    problems: list[str] = []

    for table in sorted(closure_tables - fanout_tables - allowlist):
        problems.append(
            f"UNREGISTERED patient-linked table {table!r}: it is reachable "
            "from patients(id) via FKs but the fan-out map does not know it. "
            "Fix by (a) registering an Artifact in core_service/erasure/"
            "fanout.py (deciding its DSAR export + erasability), or "
            "(b) adding it to KNOWN_NON_PHI with a written justification."
        )

    for table in sorted(fanout_tables - existing_map_tables):
        problems.append(
            f"DEAD map entry {table!r}: registered in FANOUT but the table "
            "no longer exists — remove or fix the Artifact."
        )

    for table in sorted(soft_linked - fanout_tables):
        problems.append(
            f"SOFT-LINKED PHI table {table!r} is not in FANOUT. It is linked "
            "by resource_id (no FK — the scan can't see it); its coverage is "
            "asserted by name and must never be dropped."
        )

    return problems


async def main() -> int:
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    conn = await asyncpg.connect(dsn)
    try:
        closure_tables = {r["child"] for r in await conn.fetch(CLOSURE_SQL)}
        fanout_tables = {a.table for a in FANOUT}
        existing = {
            t for t in fanout_tables
            if await conn.fetchval(EXISTS_SQL, t) == 1
        }
    finally:
        await conn.close()

    problems = run_checks(
        closure_tables=closure_tables,
        existing_map_tables=existing,
        fanout_tables=fanout_tables,
        allowlist=set(KNOWN_NON_PHI),
        soft_linked=set(SOFT_LINKED_PHI),
    )
    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 1

    print(
        f"ok: fan-out map covers the FK closure — {len(closure_tables)} "
        f"closure table(s), {len(fanout_tables)} registered artifact "
        f"table(s), {len(SOFT_LINKED_PHI)} soft-linked assertion(s), "
        f"{len(KNOWN_NON_PHI)} justified non-PHI."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
