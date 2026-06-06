"""Provider health monitor — cron every 30s."""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from audit import AuditWriter, Severity
from db import create_pool

from .. import audit_kinds
from .. import repository as repo
from ..config import settings
from ..providers import build_registry

logger = logging.getLogger(__name__)


async def run_once(
    app_pool: asyncpg.Pool,
    audit_writer: AuditWriter,
    registry,
) -> None:
    for name, provider in registry.providers.items():
        snap = await provider.health()
        async with app_pool.acquire() as conn:
            flipped = await repo.upsert_provider_health(
                conn,
                provider=name,
                healthy=snap.healthy,
                last_error=snap.last_error,
            )
        if flipped:
            await audit_writer.write_event(
                # Tenant-less event — we use a fixed system tenant marker
                # for the audit; sprint-17 will move global events to a
                # dedicated global stream.
                tenant_id=settings_system_tenant_id(),
                kind=audit_kinds.SIGNING_PROVIDER_HEALTH_CHANGED,
                actor_sub=None,
                actor_role="system",
                target_kind="signing_provider",
                target_id=None,
                payload={
                    "provider": name.value,
                    "healthy": snap.healthy,
                    "last_error": snap.last_error,
                    "latency_ms": snap.latency_ms,
                },
                severity=Severity.WARN if not snap.healthy else Severity.INFO,
            )


def settings_system_tenant_id():
    """Sprint-09 placeholder: emits health-change events under a
    fixed system tenant id so existing tenant-keyed audit infra works
    without rework. Sprint-17 may move this to a global stream."""
    from uuid import UUID

    return UUID("00000000-0000-0000-0000-00000000d0d0")


async def run_forever(*, interval_seconds: float = 30.0) -> None:
    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name="signing-service/health",
        min_size=1,
        max_size=2,
    )
    audit_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name="signing-service/health-audit",
        min_size=1,
        max_size=2,
    )
    writer = AuditWriter(audit_pool)
    registry = build_registry()
    try:
        while True:
            try:
                await run_once(app_pool, writer, registry)
            except Exception:  # noqa: BLE001
                logger.exception("health.iteration_failed")
            await asyncio.sleep(interval_seconds)
    finally:
        await registry.aclose()
        await app_pool.close()
        await audit_pool.close()
