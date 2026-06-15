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
#
# Two kinds live here:
#  1. No tenant dimension at all (global catalogues / system tables / public
#     endpoints) — there is no cross-tenant data to leak.
#  2. ADR-documented performance exceptions — these DO carry tenant_id and
#     rely on application-level filtering. They are accepted risks pending
#     security sign-off (see Sprint A1 report, OBS-A1-1x). Listed explicitly so
#     the gate stays loud about any *new* unprotected table.
EXEMPT: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("public", "schema_migrations"),  # migration tracker
        # ── global, no tenant_id ───────────────────────────────────────
        ("public", "medical_prompts"),  # global prompt catalogue (ADR-0007 RLS exception)
        (
            "public",
            "voice_commands",
        ),  # global voice-command catalogue (per-tenant overrides: sprint 17)
        (
            "public",
            "signing_provider_health",
        ),  # global signing-provider health; no tenant dimension
        ("audit", "eval_baseline"),  # global WER baseline singleton; no tenant dimension
        (
            "audit",
            "public_verify_audit",
        ),  # public/anonymous KEP verify audit; no tenant context by design
        # ── ADR-0025 perf exception (has tenant_id; app-level filtering) ─
        # FLAGGED for security/DPO sign-off — see Sprint A1 report.
        ("public", "autocomplete_rollup_progress"),
    }
)

# Name-prefix exemptions within a schema — covers range-partition children whose
# names carry a date suffix (e.g. autocomplete_telemetry_2026_05, _2026_06, …).
EXEMPT_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("public", "autocomplete_telemetry"),  # ADR-0025 perf exception (FLAGGED — see report)
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
        if any(r["schema"] == sch and r["name"].startswith(pfx) for sch, pfx in EXEMPT_PREFIXES):
            continue
        checked += 1
        full = f"{r['schema']}.{r['name']}"
        if not r["relrowsecurity"]:
            failures.append(
                f"{full}: RLS NOT enabled (ALTER TABLE {full} ENABLE ROW LEVEL SECURITY)"
            )
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
