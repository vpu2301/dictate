#!/usr/bin/env python3
"""Run dev seed SQL against the local PostgreSQL instance."""

import os
import subprocess
import sys
from pathlib import Path

SEED_SQL = Path(__file__).parent / "seed.sql"

DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "postgres")
DB_NAME = os.getenv("POSTGRES_DB", "medical_dictation")


def main() -> None:
    print(f"Seeding {DB_NAME} on {DB_HOST}:{DB_PORT}…")
    env = {**os.environ, "PGPASSWORD": DB_PASS}
    result = subprocess.run(
        [
            "psql",
            f"--host={DB_HOST}",
            f"--port={DB_PORT}",
            f"--username={DB_USER}",
            f"--dbname={DB_NAME}",
            f"--file={SEED_SQL}",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("SEED FAILED:\n", result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
    print(result.stdout)
    print("Seed complete.")


if __name__ == "__main__":
    main()
