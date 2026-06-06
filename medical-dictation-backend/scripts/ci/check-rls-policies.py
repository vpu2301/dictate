#!/usr/bin/env python3
"""CI gate: every user-schema table must have RLS enabled AND forced.

Spec § 8 risk E1: "A migration adds a new table without RLS policies;
nobody notices." This script is the wire — run after ``migrate-up`` in
CI; non-zero exit means a table is unprotected.

Detection logic:
  * Walk every relation in ``public`` and ``audit`` schemas.
  * Skip explicit exemptions (``schema_migrations`` — bookkeeping table).
  * Skip system-owned tables (the few created by extensions).
  * For each remaining table assert ``relrowsecurity = true`` AND
    ``relforcerowsecurity = true``.
  * Also count policies — a table with RLS+FORCE but no policy is fail-open
    for nobody (correct) but is almost certainly a misconfiguration; warn.

Run::

    DATABASE_URL=postgres://... uv run python scripts/ci/check-rls-policies.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Final

import asyncpg

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/medical_dictation"

# Tables that legitimately do not need RLS.
EXEMPT: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("public", "schema_migrations"),  # migration tracker
    }
)

# Schemas where we enforce. Extensions sometimes create their own schemas
# (pg_catalog, information_schema) that we never check.
ENFORCED_SCHEMAS: Final[tuple[str, ...]] = ("public", "audit")


async def main() -> int:
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT n.nspname AS schema,
                   c.relname AS name,
                   c.relrowsecurity,
                   c.relforcerowsecurity,
                   (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policy_count
            FROM pg_class c
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE c.relkind = 'r'  -- ordinary tables only
              AND n.nspname = ANY($1::text[])
            ORDER BY n.nspname, c.relname
            """,
            list(ENFORCED_SCHEMAS),
        )
    finally:
        await conn.close()

    failures: list[str] = []
    warnings: list[str] = []
    checked = 0

    for r in rows:
        key = (r["schema"], r["name"])
        if key in EXEMPT:
            continue
        checked += 1
        full = f"{r['schema']}.{r['name']}"
        if not r["relrowsecurity"]:
            failures.append(f"{full}: RLS NOT enabled (ALTER TABLE {full} ENABLE ROW LEVEL SECURITY)")
            continue
        if not r["relforcerowsecurity"]:
            failures.append(
                f"{full}: RLS enabled but NOT forced "
                f"(ALTER TABLE {full} FORCE ROW LEVEL SECURITY) — "
                "without FORCE, superuser/owner queries bypass policies"
            )
            continue
        if r["policy_count"] == 0:
            warnings.append(
                f"{full}: RLS+FORCE active but no policies → all rows hidden from everyone"
            )

    if failures:
        print("FAIL:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"PASS: {checked} table(s) in {list(ENFORCED_SCHEMAS)} all have RLS+FORCE")
    if warnings:
        print("WARNINGS (may be intentional):")
        for w in warnings:
            print(f"  - {w}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
