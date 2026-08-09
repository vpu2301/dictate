"""Erasure backup-completion notices (sprint 16 — the S11-deployment IOU).

A completed erasure destroys live data immediately, but the encrypted DB
backups only age out after ``BACKUP_RETENTION_DAYS`` (bucket ILM,
runbooks/erasure.md). The engine records that date per execution as
``report_of_execution.backups_purged_by``. This job watches for the
horizon to pass and appends the honest "fully purged from backups" line
the DPO reports to the data subject:

- ``report_of_execution.backups_purged_confirmed_at`` — stamped once
  (the presence check is the idempotence guard);
- one ``erasure.backup_horizon_reached`` audit event (sec) per request.

Hosted in the core-service lifespan behind ``MDX_BACKGROUND_JOBS``
(ADR-0041) and runnable as a CLI for external cron:

    uv run --project services/core-service \\
        python -m core_service.jobs.backup_horizon
"""

from __future__ import annotations

import logging
from uuid import UUID

import asyncpg

from audit import AuditWriter, Severity
from db import tenant_connection

from .. import audit_kinds

logger = logging.getLogger(__name__)

# Reserved global tenant (migration 0068): audit home for the per-run row.
GLOBAL_TENANT = UUID("00000000-0000-0000-0000-000000000000")

_DUE_SQL = """
SELECT id, patient_id, report_of_execution
FROM patient_privacy_requests
WHERE kind = 'erasure'
  AND status = 'completed'
  AND report_of_execution IS NOT NULL
  AND (report_of_execution ? 'backups_purged_by')
  AND (report_of_execution->>'backups_purged_by')::date <= CURRENT_DATE
  AND NOT (report_of_execution ? 'backups_purged_confirmed_at')
ORDER BY completed_at
"""


async def run_once(
    *, app_pool: asyncpg.Pool, audit_writer: AuditWriter
) -> dict[str, int]:
    """One sweep over every active tenant. Idempotent (presence guard)."""
    async with app_pool.acquire() as conn:
        tenant_rows = await conn.fetch("SELECT public.active_tenant_ids() AS id")
    tenants = [r["id"] for r in tenant_rows]

    confirmed = 0
    for tenant_id in tenants:
        async with tenant_connection(app_pool, tenant_id) as conn:
            due = await conn.fetch(_DUE_SQL)
            for row in due:
                await conn.execute(
                    """
                    UPDATE patient_privacy_requests
                    SET report_of_execution = report_of_execution
                        || jsonb_build_object(
                               'backups_purged_confirmed_at', now()::text,
                               'backups_purged_note', $2::text)
                    WHERE id = $1
                      AND NOT (report_of_execution ? 'backups_purged_confirmed_at')
                    """,
                    row["id"],
                    (
                        "all encrypted backups containing this patient's data "
                        "have expired from mdx-backups (ILM rotation complete); "
                        "the erasure is final across backups"
                    ),
                )
                confirmed += 1
        # Audit AFTER each tenant's transaction scope closes (rule 5 —
        # the audit writer runs on its own role/connection).
        for row in due:
            await audit_writer.write_event(
                tenant_id=tenant_id,
                kind=audit_kinds.ERASURE_BACKUP_HORIZON_REACHED,
                actor_sub=None,
                actor_role="scheduler",
                target_kind="patient",
                target_id=str(row["patient_id"]),
                payload={"request_id": str(row["id"])},
                severity=Severity.SEC,
            )

    summary = {"tenants_seen": len(tenants), "confirmed": confirmed}
    await audit_writer.write_event(
        tenant_id=GLOBAL_TENANT,
        kind=audit_kinds.SCHEDULER_JOB_COMPLETED,
        actor_sub=None,
        actor_role="scheduler",
        target_kind="job",
        target_id="erasure_backup_horizon",
        payload=dict(summary),
        severity=Severity.INFO,
    )
    logger.info("backup_horizon.run_completed", extra=dict(summary))
    return summary


def _main() -> None:  # pragma: no cover — manual/cron ops entrypoint
    import asyncio

    from db import create_pool

    from ..config import settings

    async def _run() -> None:
        app_pool = await create_pool(
            settings.db_app_role_dsn,
            application_name="core-service/backup-horizon",
            min_size=1,
            max_size=2,
        )
        audit_pool = await create_pool(
            settings.db_audit_writer_dsn,
            application_name="core-service/backup-horizon-audit",
            min_size=1,
            max_size=2,
        )
        try:
            summary = await run_once(
                app_pool=app_pool, audit_writer=AuditWriter(audit_pool)
            )
            print(f"backup_horizon: {summary}")
        finally:
            await app_pool.close()
            await audit_pool.close()

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    _main()
