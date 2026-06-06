#!/usr/bin/env python3
"""Seed the `templates` table with system templates from JSON files.

Idempotent: calls ``upsert_system_template()`` SQL function (defined in
migration 0014) which UPSERTs on ``(tenant_id, code, schema_version)``.

Run::

    python scripts/seed/seed_templates.py \
        --dsn postgresql://tenant_writer:tenant_writer@localhost:5432/medical_dictation
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)


async def _seed(dsn: str, seed_dir: Path) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        total = 0
        for path in sorted(seed_dir.glob("*.json")):
            doc = json.loads(path.read_text("utf-8"))
            row_id = await conn.fetchval(
                """
                SELECT upsert_system_template(
                    $1::text, $2::text, $3::text, $4::text,
                    $5::smallint, $6::jsonb
                )
                """,
                doc["code"],
                doc["name"],
                doc["language"],
                doc["specialty"],
                int(doc.get("schema_version", 1)),
                json.dumps(doc),
            )
            print(f"seeded {path.name} → {row_id}")
            total += 1
        print(f"\nTotal: {total} system templates upserted")
        return total
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn",
        default="postgresql://tenant_writer:tenant_writer@localhost:5432/medical_dictation",
    )
    parser.add_argument(
        "--seed-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "infra" / "seeds" / "templates",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(_seed(args.dsn, args.seed_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
