#!/usr/bin/env python3
"""Minimal forward/rollback SQL migration runner.

Looks at ``infra/postgres/migrations/`` and applies any file named
``NNNN_*.sql`` whose ``NNNN`` is not already recorded in
``schema_migrations``. Each ``NNNN_*.sql`` must have a sibling
``NNNN_*.down.sql`` for rollback.

The runner takes an EXCLUSIVE lock on ``schema_migrations`` for the
duration of each apply/rollback transaction, so two runners cannot race
on a shared DB. SQL files are checksummed (sha256) — a divergent
checksum on a previously-applied version aborts before any change.

Usage::

    python scripts/db/migrate.py up      # apply all pending
    python scripts/db/migrate.py down    # rollback the most recent
    python scripts/db/migrate.py status  # show applied + pending

Defaults match the dev Compose stack; override via ``DATABASE_URL``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

import asyncpg

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/medical_dictation"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "infra" / "postgres" / "migrations"

_VERSION_RE = re.compile(r"^(\d{4})_([\w-]+)\.sql$")


class Migration(NamedTuple):
    version: str
    name: str
    up_path: Path
    down_path: Path


def _discover() -> list[Migration]:
    """Return all migrations sorted by version."""
    out: list[Migration] = []
    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        if entry.name.endswith(".down.sql"):
            continue
        m = _VERSION_RE.match(entry.name)
        if not m:
            continue
        version, name = m.group(1), m.group(2)
        down = entry.with_name(f"{version}_{name}.down.sql")
        if not down.exists():
            raise RuntimeError(
                f"missing rollback for {entry.name}: expected {down.name}"
            )
        out.append(Migration(version=version, name=name, up_path=entry, down_path=down))
    return out


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _ensure_tracking_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            checksum   TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


async def _applied_versions(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
    return {r["version"]: r["checksum"] for r in rows}


async def _apply_one(conn: asyncpg.Connection, m: Migration) -> None:
    sql = m.up_path.read_text()
    csum = _checksum(m.up_path)
    async with conn.transaction():
        await conn.execute("LOCK TABLE schema_migrations IN EXCLUSIVE MODE")
        existing = await conn.fetchrow(
            "SELECT checksum FROM schema_migrations WHERE version = $1", m.version
        )
        if existing is not None:
            if existing["checksum"] != csum:
                raise RuntimeError(
                    f"checksum drift on {m.version}: applied={existing['checksum'][:8]}…, "
                    f"file={csum[:8]}…"
                )
            print(f"  = {m.version}_{m.name} (already applied)")
            return
        await conn.execute(sql)
        await conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
            m.version,
            m.name,
            csum,
        )
    print(f"  + {m.version}_{m.name}")


async def _rollback_one(conn: asyncpg.Connection, m: Migration) -> None:
    sql = m.down_path.read_text()
    async with conn.transaction():
        await conn.execute("LOCK TABLE schema_migrations IN EXCLUSIVE MODE")
        existing = await conn.fetchrow(
            "SELECT version FROM schema_migrations WHERE version = $1", m.version
        )
        if existing is None:
            print(f"  - {m.version}_{m.name} (not applied; nothing to do)")
            return
        await conn.execute(sql)
        await conn.execute("DELETE FROM schema_migrations WHERE version = $1", m.version)
    print(f"  - {m.version}_{m.name}")


async def cmd_up(conn: asyncpg.Connection) -> int:
    await _ensure_tracking_table(conn)
    migrations = _discover()
    applied = await _applied_versions(conn)
    pending = [m for m in migrations if m.version not in applied]
    if not pending:
        print("No pending migrations.")
        return 0
    print(f"Applying {len(pending)} migration(s):")
    for m in pending:
        await _apply_one(conn, m)
    return 0


async def cmd_down(conn: asyncpg.Connection) -> int:
    """Rollback the single most-recently-applied migration."""
    await _ensure_tracking_table(conn)
    row = await conn.fetchrow(
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    )
    if row is None:
        print("Nothing to roll back.")
        return 0
    target_version = row["version"]
    migration = next(
        (m for m in _discover() if m.version == target_version),
        None,
    )
    if migration is None:
        print(
            f"ERROR: version {target_version} is recorded as applied but the migration "
            "file is missing from infra/postgres/migrations/",
            file=sys.stderr,
        )
        return 2
    await _rollback_one(conn, migration)
    return 0


async def cmd_status(conn: asyncpg.Connection) -> int:
    await _ensure_tracking_table(conn)
    migrations = _discover()
    applied = await _applied_versions(conn)
    print(f"{'STATUS':<8} {'VERSION':<6} NAME")
    for m in migrations:
        status = "applied" if m.version in applied else "pending"
        print(f"{status:<8} {m.version:<6} {m.name}")
    # Highlight checksum drift if any
    for version, csum in applied.items():
        match = next((x for x in migrations if x.version == version), None)
        if match is not None and _checksum(match.up_path) != csum:
            print(
                f"WARNING: checksum drift on {version} — file edited after apply.",
                file=sys.stderr,
            )
    return 0


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("up", "down", "status"))
    p.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL", DEFAULT_DSN),
        help="Postgres DSN (default: env DATABASE_URL or %(default)s)",
    )
    args = p.parse_args()

    conn = await asyncpg.connect(args.dsn)
    try:
        if args.command == "up":
            return await cmd_up(conn)
        if args.command == "down":
            return await cmd_down(conn)
        if args.command == "status":
            return await cmd_status(conn)
        return 2  # unreachable
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
