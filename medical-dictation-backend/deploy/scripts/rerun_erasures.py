"""Post-restore erasure re-run (S11 deployment, ADR-0028).

A database restore resurrects data whose erasure completed AFTER the
backup was taken. This script makes the restore runbook's "re-run
completed erasures" step executable instead of manual archaeology:

    python rerun_erasures.py --ledger ledger.json --backup-taken-at ISO

The ledger is the pre-restore snapshot of completed erasure requests
(captured by restore.sh, or by a standing export if the live DB died).
For every entry with ``completed_at > backup-taken-at``:

1. skip when the restored DB has no such patient row (nothing came back);
2. force the restored request row to ``executing`` (upsert — the row may
   have been restored in an earlier state or not at all), carrying the
   original requester/reviewer so the two-person CHECKs stay honest;
3. run the idempotent erasure engine (its crashed-``executing`` re-run
   path), which destroys everything again and re-stamps
   ``report_of_execution`` — operator ``restore-rerun``, fully audited.

Runs inside the privacy-ops erasure job container (the only runtime
that holds DB_ERASURE_DSN); the row surgery in step 2 uses the
operational scan DSN (DATABASE_URL), same as the scheduler's sweep.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from uuid import UUID

import asyncpg

UPSERT_EXECUTING = """
INSERT INTO patient_privacy_requests
    (id, tenant_id, patient_id, kind, reason, status,
     requested_by, requested_at, reviewed_by, reviewed_at,
     scheduled_for, executing_at)
VALUES ($1, $2, $3, 'erasure', $4, 'executing',
        $5, $6, $7, $8, $9, now())
ON CONFLICT (id) DO UPDATE SET
    status       = 'executing',
    executing_at = now(),
    reviewed_by  = EXCLUDED.reviewed_by,
    reviewed_at  = EXCLUDED.reviewed_at,
    last_error   = NULL
"""


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, help="pre-restore ledger JSON")
    parser.add_argument("--backup-taken-at", required=True, help="manifest taken_at (ISO)")
    args = parser.parse_args()

    backup_taken_at = _ts(args.backup_taken_at)
    with open(args.ledger) as fh:
        ledger = json.load(fh)

    due = [e for e in ledger if _ts(e["completed_at"]) > backup_taken_at]
    print(
        f"ledger: {len(ledger)} completed erasure(s); "
        f"{len(due)} completed after backup {backup_taken_at.isoformat()} → re-run"
    )
    if not due:
        print("ok: nothing to re-run")
        return 0

    from core_service.erasure.engine import (
        ErasureRefusedError,
        ErasureRuntime,
        execute_erasure,
    )

    ops = await asyncpg.connect(
        os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/medical_dictation",
        )
    )
    runtime: ErasureRuntime | None = None
    failures = 0
    try:
        for entry in due:
            request_id = UUID(entry["id"])
            patient_id = UUID(entry["patient_id"])
            tenant_id = UUID(entry["tenant_id"])

            restored = await ops.fetchval(
                "SELECT count(*) FROM patients WHERE id = $1", patient_id
            )
            if not restored:
                print(f"skip: request={request_id} — patient not in restored DB")
                continue

            try:
                await ops.execute(
                    UPSERT_EXECUTING,
                    request_id, tenant_id, patient_id,
                    entry.get("reason") or "",
                    UUID(entry["requested_by"]) if entry.get("requested_by") else None,
                    _ts(entry["requested_at"]),
                    UUID(entry["reviewed_by"]) if entry.get("reviewed_by") else None,
                    _ts(entry["reviewed_at"]) if entry.get("reviewed_at") else None,
                    _ts(entry["scheduled_for"]) if entry.get("scheduled_for") else None,
                )
            except asyncpg.PostgresError as exc:
                failures += 1
                print(
                    f"FAILED upsert: request={request_id} {type(exc).__name__}: {exc}"
                    " — resolve manually (missing reviewer user?)",
                    file=sys.stderr,
                )
                continue

            if runtime is None:
                runtime = await ErasureRuntime.build()
            try:
                report = await execute_erasure(
                    runtime,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    operator="restore-rerun",
                )
                print(
                    f"re-erased: request={request_id} patient={patient_id} "
                    f"destroyed={report['counts']['destroyed']} "
                    f"retained={report['counts']['retained']} "
                    f"backups_purged_by={report['backups_purged_by']}"
                )
            except ErasureRefusedError as exc:
                failures += 1
                print(f"REFUSED: request={request_id} [{exc.code}]", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 — keep sweeping, last_error recorded
                failures += 1
                print(
                    f"FAILED: request={request_id} {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
    finally:
        await ops.close()

    print(f"ok: rerun processed {len(due)} request(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
