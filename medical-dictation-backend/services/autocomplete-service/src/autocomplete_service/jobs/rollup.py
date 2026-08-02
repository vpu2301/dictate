"""Nightly telemetry roll-up.

Aggregates yesterday's telemetry per (tenant, phrase) into the phrase
counters, bumps the per-tenant ``version_tag`` so the trie cache
rebuilds with updated ranking on the next request.

Cron at 03:30 UTC. Idempotent via ``autocomplete_rollup_progress``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import asyncpg

from audit import AuditWriter, Severity
from db import create_pool, tenant_connection

from .. import audit_kinds
from .. import repository as repo
from ..config import settings

logger = logging.getLogger(__name__)

# Read by the observable gauge ``mdx_autocomplete_rollup_last_run_unix_ts``
# (registered in main_deps; the RollupStale alert fires on its age).
_last_success_unix: float = 0.0
# Read by ``mdx_autocomplete_corpus_size{source}`` — refreshed each roll-up.
_corpus_size: dict[str, int] = {}


def last_success_unix() -> float:
    return _last_success_unix


def corpus_size_by_source() -> dict[str, int]:
    return dict(_corpus_size)


# Sprint 15: Layer C acceptance rate — the feature's quality metric and the
# kill-switch input (ADR-0036). Refreshed each roll-up from layer_c telemetry
# rows; global (not per-tenant) to bound label cardinality. Read by the
# observable gauges registered in main_deps.
_layer_c_events: dict[str, int] = {}


def layer_c_events_by_type() -> dict[str, int]:
    return dict(_layer_c_events)


def layer_c_acceptance_rate() -> float:
    impressions = sum(
        _layer_c_events.get(e, 0) for e in ("shown_only", "accepted", "rejected")
    )
    if impressions == 0:
        return 0.0
    return _layer_c_events.get("accepted", 0) / impressions


async def rollup_all(
    *,
    app_pool: asyncpg.Pool,
    audit_writer: AuditWriter,
    redis,
    day: date | None = None,
) -> int:
    # asyncpg binds ``$1::date`` params as dates — a str raises DataError,
    # so the day travels as a ``date`` object end-to-end.
    day_obj = day or (datetime.now(UTC).date() - timedelta(days=1))
    day_iso = day_obj.isoformat()
    total_updated = 0
    async with app_pool.acquire() as conn:
        tenants = await conn.fetch(
            "SELECT DISTINCT tenant_id FROM autocomplete_telemetry "
            "WHERE created_at >= $1::date AND created_at < ($1::date + interval '1 day')",
            day_obj,
        )
    for t in tenants:
        tid: UUID = t["tenant_id"]
        async with tenant_connection(app_pool, tid) as conn:
            updated = await repo.rollup_tenant_day(conn, tenant_id=tid, day=day_obj)
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
    # Corpus-size gauge refresh (dashboard row 4: corpus by source).
    # MUST go through tenant_connection: querying an RLS table on a bare
    # pooled connection evaluates the policy with the GUC's session reset
    # value — an EMPTY STRING once any prior txn set it — and ''::uuid
    # aborts the whole roll-up. The nil tenant matches no tenant rows, so
    # this counts exactly the system corpus.
    nil_tenant = UUID("00000000-0000-0000-0000-000000000000")
    async with tenant_connection(app_pool, nil_tenant) as conn:
        rows = await conn.fetch(
            "SELECT source::text, count(*) AS n FROM autocomplete_phrases "
            "WHERE enabled = TRUE GROUP BY source"
        )
    # Layer C acceptance-rate refresh (sprint 15). The telemetry table has
    # no RLS (ADR-0025 exception), so a bare pooled connection is correct
    # here — same as the TelemetryBuffer's insert path.
    async with app_pool.acquire() as conn:
        lc_rows = await conn.fetch(
            "SELECT event_type, count(*) AS n FROM autocomplete_telemetry "
            "WHERE source = 'layer_c' "
            "  AND created_at >= $1::date AND created_at < ($1::date + interval '1 day') "
            "GROUP BY event_type",
            day_obj,
        )
    _layer_c_events.clear()
    _layer_c_events.update({r["event_type"]: int(r["n"]) for r in lc_rows})
    global _last_success_unix
    _corpus_size.clear()
    _corpus_size.update({r["source"]: int(r["n"]) for r in rows})
    _last_success_unix = datetime.now(UTC).timestamp()
    logger.info(
        "rollup.completed",
        extra={"rollup_date": day_iso, "tenants": len(tenants), "phrases_updated": total_updated},
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


def _main() -> None:  # pragma: no cover — manual ops entrypoint
    """Manual run: uv run --project services/autocomplete-service \
    python -m autocomplete_service.jobs.rollup [--day YYYY-MM-DD]

    NEVER bypass the ``autocomplete_rollup_progress`` guard for a re-run —
    counters are monotonic increments; replaying a day double-counts.
    Delete the progress row for (tenant, day) ONLY when the original run
    is known to have failed before updating counters (see the runbook).
    """
    import argparse

    from redis.asyncio import Redis

    parser = argparse.ArgumentParser(description="Run the telemetry roll-up once")
    parser.add_argument("--day", type=date.fromisoformat, default=None,
                        help="UTC day to roll up (default: yesterday)")
    args = parser.parse_args()

    async def _run() -> None:
        app_pool = await create_pool(
            settings.db_app_role_dsn,
            application_name="autocomplete-rollup-manual", min_size=1, max_size=2)
        audit_pool = await create_pool(
            settings.db_audit_writer_dsn,
            application_name="autocomplete-rollup-manual-audit", min_size=1, max_size=2)
        redis = Redis.from_url(settings.redis_url, decode_responses=False)
        try:
            n = await rollup_all(
                app_pool=app_pool, audit_writer=AuditWriter(audit_pool),
                redis=redis, day=args.day)
            print(f"rollup complete: {n} phrase(s) updated")
        finally:
            await redis.aclose()
            await app_pool.close()
            await audit_pool.close()

    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    _main()
