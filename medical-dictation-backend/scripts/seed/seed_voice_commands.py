#!/usr/bin/env python3
"""Seed the `voice_commands` table from the JSON fixtures.

Idempotent: deletes the (intent, language) row before inserting the
new fixtures, so re-running picks up edits cleanly.

Usage::

    python scripts/seed/seed_voice_commands.py \
        --dsn postgresql://postgres:postgres@localhost:5432/medical_dictation
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


async def _seed(dsn: str, fixtures_dir: Path) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        total = 0
        for path in sorted(fixtures_dir.glob("voice_commands_*.json")):
            language = path.stem.split("_")[-1]
            commands = json.loads(path.read_text("utf-8"))
            # Clean slate for this language so re-seeds drop removed commands.
            await conn.execute("DELETE FROM voice_commands WHERE language = $1", language)
            for cmd in commands:
                await conn.execute(
                    """
                    INSERT INTO voice_commands
                        (intent, language, phrases,
                         requires_pause_before_ms, min_avg_probability,
                         is_section_command)
                    VALUES ($1, $2, $3::jsonb, $4, $5, $6)
                    """,
                    cmd["intent"],
                    language,
                    json.dumps(cmd["phrases"]),
                    int(cmd.get("requires_pause_before_ms", 200)),
                    float(cmd.get("min_avg_probability", 0.85)),
                    bool(cmd.get("is_section_command", False)),
                )
                total += 1
            print(f"seeded {language}: {len(commands)} commands")
        return total
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn",
        default="postgresql://postgres:postgres@localhost:5432/medical_dictation",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "infra" / "postgres" / "seed",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    total = asyncio.run(_seed(args.dsn, args.fixtures_dir))
    print(f"total: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
