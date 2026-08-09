"""Idle-draft cleanup (auto-archive).

Sprint-08 shipped the SQL + the service method; sprint-16 attaches it to
the scheduler (``run_for_all_tenants`` hosted in the service lifespan
behind ``MDX_BACKGROUND_JOBS``, plus the ``python -m`` CLI for external
cron — ADR-0041).

Policy (spec §4.4): drafts untouched for 30 days transition to
``cancelled`` with reason ``auto_archive_idle_draft``. The owning
tenant is preserved in the audit event so DPO can re-open within 90
days if needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import asyncpg

from audit import AuditWriter, Severity
from db import tenant_connection

from .. import audit_kinds

logger = logging.getLogger(__name__)

# Reserved global tenant (migration 0068): the audit home for
# fleet-level scheduler runs that belong to no clinical tenant.
GLOBAL_TENANT = UUID("00000000-0000-0000-0000-000000000000")


@dataclass(slots=True)
class CleanupResult:
    archived: list[UUID]


async def auto_archive_idle_drafts(
    *,
    app_pool: asyncpg.Pool,
    audit_writer: AuditWriter,
    tenant_id: UUID,
    idle_for: timedelta = timedelta(days=30),
) -> CleanupResult:
    archived: list[UUID] = []
    async with tenant_connection(app_pool, tenant_id) as conn:
        rows = await conn.fetch(
            """
            UPDATE reports
            SET status            = 'cancelled',
                cancelled_at      = now(),
                cancelled_reason  = 'auto_archive_idle_draft',
                updated_at        = now()
            WHERE tenant_id = $1
              AND status    = 'draft'
              AND updated_at < now() - $2::interval
            RETURNING id
            """,
            tenant_id,
            idle_for,
        )
        archived = [r["id"] for r in rows]

    for rid in archived:
        await audit_writer.write_event(
            tenant_id=tenant_id,
            kind=audit_kinds.REPORT_CANCELLED,
            actor_sub=None,
            actor_role="system",
            target_kind="report",
            target_id=rid,
            payload={"reason": "auto_archive_idle_draft"},
            severity=Severity.INFO,
        )
    return CleanupResult(archived=archived)


async def run_for_all_tenants(
    *,
    app_pool: asyncpg.Pool,
    audit_writer: AuditWriter,
    idle_for: timedelta = timedelta(days=30),
) -> dict[str, int]:
    """One scheduler iteration: sweep every active tenant.

    Tenant enumeration goes through the SECURITY DEFINER
    ``active_tenant_ids()`` (migration 0071) because ``tenants`` is
    RLS-FORCEd to self-select for app_role. Idempotent by construction —
    an already-archived draft no longer matches ``status = 'draft'``.

    Emits one ``scheduler.job.completed`` audit row per run (global
    tenant), carrying per-tenant counts; per-report events are written by
    :func:`auto_archive_idle_drafts` under the owning tenant as before.
    """
    async with app_pool.acquire() as conn:
        tenant_rows = await conn.fetch("SELECT public.active_tenant_ids() AS id")
    tenants = [r["id"] for r in tenant_rows]

    archived_total = 0
    tenants_with_work = 0
    for tenant_id in tenants:
        result = await auto_archive_idle_drafts(
            app_pool=app_pool,
            audit_writer=audit_writer,
            tenant_id=tenant_id,
            idle_for=idle_for,
        )
        if result.archived:
            tenants_with_work += 1
            archived_total += len(result.archived)

    summary = {
        "tenants_seen": len(tenants),
        "tenants_with_work": tenants_with_work,
        "archived": archived_total,
    }
    await audit_writer.write_event(
        tenant_id=GLOBAL_TENANT,
        kind=audit_kinds.SCHEDULER_JOB_COMPLETED,
        actor_sub=None,
        actor_role="scheduler",
        target_kind="job",
        target_id="idle_draft_cleanup",
        payload=dict(summary),
        severity=Severity.INFO,
    )
    logger.info("idle_draft_cleanup.run_completed", extra=dict(summary))
    return summary


def _main() -> None:  # pragma: no cover — manual/cron ops entrypoint
    """Manual run: uv run --project services/report-service \
    python -m report_service.jobs.idle_draft_cleanup"""
    import asyncio

    from db import create_pool

    from ..config import settings

    async def _run() -> None:
        app_pool = await create_pool(
            settings.db_app_role_dsn,
            application_name="report-service/idle-draft-cleanup",
            min_size=1,
            max_size=2,
        )
        audit_pool = await create_pool(
            settings.db_audit_writer_dsn,
            application_name="report-service/idle-draft-cleanup-audit",
            min_size=1,
            max_size=2,
        )
        try:
            summary = await run_for_all_tenants(
                app_pool=app_pool,
                audit_writer=AuditWriter(audit_pool),
                idle_for=timedelta(days=settings.idle_draft_days),
            )
            print(f"idle_draft_cleanup: {summary}")
        finally:
            await app_pool.close()
            await audit_pool.close()

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    _main()
