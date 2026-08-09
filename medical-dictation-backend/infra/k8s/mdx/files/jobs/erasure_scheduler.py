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
import time

import asyncpg

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/medical_dictation"


def _setup_metrics():
    """Push-style OTLP metrics for a short-lived cron process (S11 step 08).

    Best-effort: an unreachable collector must never fail the sweep. The
    gauges go stale between runs by design — the alert rules use
    ``max_over_time`` lookback windows sized to the 15-minute cadence.
    """
    try:
        from opentelemetry import metrics
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint, insecure=True),
            export_interval_millis=60_000,
        )
        provider = MeterProvider(
            resource=Resource.create({"service.name": "erasure-scheduler"}),
            metric_readers=[reader],
        )
        metrics.set_meter_provider(provider)
        return provider
    except Exception as exc:  # noqa: BLE001 — metrics are advisory
        print(f"warn: metrics disabled ({type(exc).__name__})", file=sys.stderr)
        return None


async def _emit_queue_gauges(scan: asyncpg.Connection) -> None:
    """The DPO's queue view + the alert inputs. Labels: enums only."""
    from opentelemetry import metrics

    meter = metrics.get_meter("erasure-scheduler")
    requests = meter.create_gauge("mdx_privacy_requests", description="Privacy requests by kind/status")
    oldest_exec = meter.create_gauge(
        "mdx_privacy_oldest_executing_age_seconds",
        unit="s", description="Age of the oldest executing request per kind",
    )
    overdue = meter.create_gauge(
        "mdx_privacy_overdue_approved_count",
        description="Approved erasures still unexecuted 24h past scheduled_for",
    )
    errors = meter.create_gauge(
        "mdx_privacy_requests_with_error_count",
        description="Requests carrying last_error (stuck executions)",
    )
    # 'failed' rows are permanent history — a recovered patient gets a NEW
    # completed request. Alerting must count only failures no newer export
    # has superseded, or DsarExportFailed would fire forever (ADR-0028).
    dsar_failed_unresolved = meter.create_gauge(
        "mdx_privacy_dsar_failed_unresolved_count",
        description="Failed DSAR requests with no newer completed export for the same patient",
    )
    last_run = meter.create_gauge(
        "mdx_erasure_scheduler_last_run_unix_ts", description="Scheduler heartbeat"
    )

    for row in await scan.fetch(
        "SELECT kind, status, count(*) AS n FROM patient_privacy_requests GROUP BY 1, 2"
    ):
        requests.set(row["n"], attributes={"kind": row["kind"], "status": row["status"]})
    for row in await scan.fetch(
        "SELECT kind, EXTRACT(EPOCH FROM (now() - min(executing_at)))::bigint AS age "
        "FROM patient_privacy_requests WHERE status = 'executing' "
        "AND executing_at IS NOT NULL GROUP BY kind"
    ):
        oldest_exec.set(row["age"], attributes={"kind": row["kind"]})
    overdue.set(
        await scan.fetchval(
            "SELECT count(*) FROM patient_privacy_requests WHERE kind = 'erasure' "
            "AND status = 'approved' AND scheduled_for < now() - interval '24 hours'"
        )
    )
    errors.set(
        await scan.fetchval(
            "SELECT count(*) FROM patient_privacy_requests WHERE last_error IS NOT NULL"
        )
    )
    dsar_failed_unresolved.set(
        await scan.fetchval(
            """
            SELECT count(*) FROM patient_privacy_requests f
            WHERE f.kind = 'dsar' AND f.status = 'failed'
              AND NOT EXISTS (
                SELECT 1 FROM patient_privacy_requests n
                WHERE n.patient_id = f.patient_id AND n.kind = 'dsar'
                  AND n.status = 'completed'
                  AND n.requested_at > f.requested_at)
            """
        )
    )
    last_run.set(int(time.time()))


async def main() -> int:

    provider = _setup_metrics()
    try:
        return await _run(provider)
    finally:
        if provider is not None:
            provider.force_flush()
            provider.shutdown()


async def _run(provider) -> int:
    from core_service.erasure.engine import (
        ErasureRefusedError,
        ErasureRuntime,
        execute_erasure,
    )

    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    scan = await asyncpg.connect(dsn)
    try:
        try:
            await _emit_queue_gauges(scan)
        except Exception as exc:  # noqa: BLE001 — metrics are advisory
            print(f"warn: queue gauges failed ({type(exc).__name__})", file=sys.stderr)
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
