"""Idle-draft cleanup (auto-archive).

Sprint-08 ships the SQL + the service method; sprint-16 attaches it to
the scheduler.

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
