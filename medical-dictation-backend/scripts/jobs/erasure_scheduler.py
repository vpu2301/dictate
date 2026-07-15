"""Erasure scheduler (S11 step 07) — cron, every 15 minutes.

Finds approved erasure requests whose grace period has elapsed
(cross-tenant scan under an operational DSN — the sweep is the point)
and executes them SERIALLY through the same engine + advisory lock the
manual entry point uses (erasure volume is tiny; serialization avoids
object/DEK races).

    uv run --project services/core-service python scripts/jobs/erasure_scheduler.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/medical_dictation"


async def main() -> int:
    from core_service.erasure.engine import ErasureRefusedError, ErasureRuntime, execute_erasure

    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    scan = await asyncpg.connect(dsn)
    try:
        due = await scan.fetch(
            """
            SELECT id, tenant_id FROM patient_privacy_requests
            WHERE kind = 'erasure'
              AND ((status = 'approved' AND scheduled_for <= now())
                   OR status = 'executing')  -- crashed runs: re-run to completion
            ORDER BY scheduled_for
            """
        )
    finally:
        await scan.close()

    if not due:
        print("ok: no due erasure requests")
        return 0

    runtime = await ErasureRuntime.build()
    failures = 0
    for row in due:
        try:
            report = await execute_erasure(
                runtime,
                tenant_id=row["tenant_id"],
                request_id=row["id"],
                operator="scheduler",
            )
            print(
                f"executed: request={row['id']} destroyed={report['counts']['destroyed']} "
                f"retained={report['counts']['retained']}"
            )
        except ErasureRefusedError as exc:
            print(f"skipped: request={row['id']} [{exc.code}]")
        except Exception as exc:  # noqa: BLE001 — keep sweeping; last_error is recorded
            failures += 1
            print(f"FAILED: request={row['id']} {type(exc).__name__}: {exc}", file=sys.stderr)

    print(f"ok: erasure-scheduler processed {len(due)} request(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
