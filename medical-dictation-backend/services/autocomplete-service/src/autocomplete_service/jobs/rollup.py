"""Nightly telemetry roll-up.

Aggregates yesterday's telemetry per (tenant, phrase) into the phrase
counters, bumps the per-tenant ``version_tag`` so the trie cache
rebuilds with updated ranking on the next request.

Cron at 03:30 UTC. Idempotent via ``autocomplete_rollup_progress``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import asyncpg

from audit import AuditWriter, Severity
from db import create_pool, tenant_connection

from .. import audit_kinds, repository as repo
from ..config import settings

logger = logging.getLogger(__name__)


async def rollup_all(
    *,
    app_pool: asyncpg.Pool,
    audit_writer: AuditWriter,
    redis,
    day: date | None = None,
) -> int:
    day_iso = (day or (datetime.now(timezone.utc).date() - timedelta(days=1))).isoformat()
    total_updated = 0
    async with app_pool.acquire() as conn:
        tenants = await conn.fetch(
            "SELECT DISTINCT tenant_id FROM autocomplete_telemetry "
            "WHERE created_at >= $1::date AND created_at < ($1::date + interval '1 day')",
            day_iso,
        )
    for t in tenants:
        tid: UUID = t["tenant_id"]
        async with tenant_connection(app_pool, tid) as conn:
            updated = await repo.rollup_tenant_day(conn, tenant_id=tid, day_iso=day_iso)
        total_updated += updated
        if updated > 0:
            # Bump the trie cache version_tag so the next request
            # rebuilds with the new counters.
            await redis.incr(f"autocomplete:tenant_phrase_version:{tid}")
        await audit_writer.write_event(
            tenant_id=tid,
            kind=audit_kinds.ROLLUP_COMPLETED,
            actor_sub=None,
            actor_role="system",
            target_kind="autocomplete_phrases",
            target_id=None,
            payload={"rollup_date": day_iso, "phrases_updated": updated},
            severity=Severity.INFO,
        )
    return total_updated


async def run_forever(*, interval_seconds: float = 86400.0) -> None:  # pragma: no cover
    from redis.asyncio import Redis

    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name="autocomplete-service/rollup",
        min_size=1,
        max_size=2,
    )
    audit_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name="autocomplete-service/rollup-audit",
        min_size=1,
        max_size=2,
    )
    writer = AuditWriter(audit_pool)
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    try:
        while True:
            try:
                await rollup_all(app_pool=app_pool, audit_writer=writer, redis=redis)
            except Exception:  # noqa: BLE001
                logger.exception("rollup.iteration_failed")
            await asyncio.sleep(interval_seconds)
    finally:
        await redis.aclose()
        await app_pool.close()
        await audit_pool.close()
