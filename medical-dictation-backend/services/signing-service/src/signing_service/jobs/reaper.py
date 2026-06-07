"""Session reaper — cron every 60s.

Expires sessions past their TTL (with a 60s grace) and flags
``verifying`` sessions that have been stuck for more than 5 minutes.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from audit import AuditWriter, Severity
from db import create_pool, tenant_connection

from .. import audit_kinds
from .. import repository as repo
from ..config import settings

logger = logging.getLogger(__name__)


async def run_once(app_pool: asyncpg.Pool, audit_writer: AuditWriter) -> int:
    """One iteration. Returns total session transitions made."""
    transitioned = 0
    async with app_pool.acquire() as conn:
        # Read across all tenants — reaper bypasses tenant scope.
        await conn.execute(
            "SET LOCAL row_security TO off"
        )  # privileged; reaper runs on app_role with NO RLS bypass in prod
        tenants = await conn.fetch(
            "SELECT DISTINCT tenant_id FROM signing_sessions "
            "WHERE status IN ('initiating','awaiting_user','verifying')"
        )
    for t in tenants:
        async with tenant_connection(app_pool, t["tenant_id"]) as conn:
            expired = await repo.expire_due_sessions(conn)
            stuck = await repo.mark_stuck_verifying(conn)
        for sid in expired:
            transitioned += 1
            await audit_writer.write_event(
                tenant_id=t["tenant_id"],
                kind=audit_kinds.SIGNING_SESSION_EXPIRED,
                actor_sub=None,
                actor_role="system",
                target_kind="signing_session",
                target_id=sid,
                payload={"reason": "reaper_ttl"},
                severity=Severity.INFO,
            )
        for sid in stuck:
            transitioned += 1
            await audit_writer.write_event(
                tenant_id=t["tenant_id"],
                kind=audit_kinds.SIGNING_SESSION_FAILED,
                actor_sub=None,
                actor_role="system",
                target_kind="signing_session",
                target_id=sid,
                payload={"reason": "verification_stuck_5min"},
                severity=Severity.WARN,
            )
    return transitioned


async def run_forever(*, interval_seconds: float = 60.0) -> None:
    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name="signing-service/reaper",
        min_size=1,
        max_size=2,
    )
    audit_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name="signing-service/reaper-audit",
        min_size=1,
        max_size=2,
    )
    writer = AuditWriter(audit_pool)
    try:
        while True:
            try:
                await run_once(app_pool, writer)
            except Exception:  # noqa: BLE001
                logger.exception("reaper.iteration_failed")
            await asyncio.sleep(interval_seconds)
    finally:
        await app_pool.close()
        await audit_pool.close()
