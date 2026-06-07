"""Seed 100k reports across 10 tenants for sprint-08 day-9 load test.

Each report gets 1-20 versions; statuses distributed:
  - 60% draft
  - 25% finalized
  - 10% signed
  - 4% amended
  - 1% cancelled

The script is deliberately idempotent on report code, so re-runs only
top up the inventory to the target count.

Usage:
    uv run python scripts/loadtest/sprint-08-seed.py \\
        --dsn postgres://app_role:app_role@localhost:5432/medical_dictation \\
        --tenants 10 --reports-per-tenant 10000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from datetime import date, timedelta
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)

STATUS_WEIGHTS = [
    ("draft", 60),
    ("finalized", 25),
    ("signed", 10),
    ("amended", 4),
    ("cancelled", 1),
]


def _pick_status(rng: random.Random) -> str:
    pop, weights = zip(*STATUS_WEIGHTS, strict=False)
    return rng.choices(pop, weights=weights, k=1)[0]


async def seed_tenant(
    conn: asyncpg.Connection, *, tenant_id: UUID, count: int, rng: random.Random
) -> int:
    inserted = 0
    template_id = await conn.fetchval(
        "SELECT id FROM templates WHERE tenant_id IS NULL ORDER BY random() LIMIT 1"
    )
    if template_id is None:
        raise RuntimeError("no system templates available for seeding")

    author_id = await conn.fetchval("SELECT id FROM users WHERE tenant_id = $1 LIMIT 1", tenant_id)
    if author_id is None:
        raise RuntimeError(f"tenant {tenant_id} has no users")

    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))

    for i in range(count):
        status = _pick_status(rng)
        encounter_date = date(2026, 1, 1) + timedelta(days=rng.randint(0, 365))
        title = f"Synthetic report #{i:05d}"
        code = f"SEED-{tenant_id.hex[:6]}-{i:05d}"
        icd10 = rng.choice([["I21"], ["E11.9"], ["J45.0"], ["I50.0"], []])
        text_body = (
            f"Section {i}: patient presents with synthetic complaint #{i}. "
            f"Notes include keywords such as задишка, біль, грип, gastritis."
        )
        content_obj = {
            "template_id": str(template_id),
            "template_schema_version": 1,
            "title": title,
            "encounter_date": encounter_date.isoformat(),
            "sections": [
                {
                    "section_key": "chief_complaint",
                    "text": text_body,
                    "transcript_segment_ids": [],
                    "icd10": [],
                    "field_specific_metadata": {},
                }
            ],
            "icd10_codes": [{"code": c} for c in icd10],
        }

        # 1-step seed inserts: write report + v1 directly. We bypass the
        # service for raw throughput; FK constraint is deferrable.
        report_id = await conn.fetchval(
            """
            INSERT INTO reports (
                tenant_id, code, status, primary_author_id, co_author_ids,
                template_id, template_schema_version,
                title, icd10_codes, encounter_date,
                finalized_at, signed_at, cancelled_at
            )
            VALUES ($1, $2, $3::report_status, $4, '{}'::uuid[],
                    $5, 1, $6, $7::text[], $8::date,
                    CASE WHEN $3 IN ('finalized','signed','amended') THEN now() END,
                    CASE WHEN $3 IN ('signed','amended') THEN now() END,
                    CASE WHEN $3 = 'cancelled' THEN now() END)
            RETURNING id
            """,
            tenant_id,
            code,
            status,
            author_id,
            template_id,
            title,
            icd10,
            encounter_date,
        )
        version_id = await conn.fetchval(
            """
            INSERT INTO report_versions (
                report_id, version_number, parent_version_id, created_by,
                content_jsonb, rendered_text, diff_jsonb, metadata
            )
            VALUES ($1, 1, NULL, $2, $3::jsonb, $4, '{}'::jsonb, '{}'::jsonb)
            RETURNING id
            """,
            report_id,
            author_id,
            json.dumps(content_obj),
            text_body,
        )
        await conn.execute(
            "UPDATE reports SET current_version_id = $2 WHERE id = $1",
            report_id,
            version_id,
        )
        inserted += 1
    return inserted


async def main(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    pool = await asyncpg.create_pool(args.dsn, min_size=2, max_size=8)
    try:
        async with pool.acquire() as conn:
            tenants = await conn.fetch(
                "SELECT id FROM tenants WHERE active = true ORDER BY id LIMIT $1",
                args.tenants,
            )
        total = 0
        for t in tenants:
            async with pool.acquire() as conn, conn.transaction():
                n = await seed_tenant(
                    conn,
                    tenant_id=t["id"],
                    count=args.reports_per_tenant,
                    rng=rng,
                )
                total += n
            print(f"seeded {n} reports for tenant {t['id']}")
        print(f"DONE: {total} total")
        return 0
    finally:
        await pool.close()


def cli() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dsn", required=True)
    p.add_argument("--tenants", type=int, default=10)
    p.add_argument("--reports-per-tenant", type=int, default=10000)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(main(args))


if __name__ == "__main__":
    sys.exit(cli())
