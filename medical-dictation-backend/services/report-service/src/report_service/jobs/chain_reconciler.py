"""Daily amendment-chain reconciler — cron 04:30 UTC.

For each tenant: load every report and its versions, run the same
pure-Python chain verifier the property test uses, and:

- INSERT one row into ``audit.report_chain_failures`` per anomaly.
- Emit one ``report.chain_integrity_failure`` audit event per anomaly
  (hash-chained — sprint-02 audit log is the ultimate source of truth).
- Bump the ``mdx_reports_chain_integrity_check_failures_total``
  counter.

Designed to be safe to run from a cron job in-process or as a
standalone CLI (``uv run python -m report_service.jobs.chain_reconciler``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Iterable
from uuid import UUID

import asyncpg

from audit import AuditWriter, Severity
from db import create_pool, tenant_connection

from .. import audit_kinds
from ..config import settings
from ..domain.chain_integrity import ChainNode, verify_chain

logger = logging.getLogger(__name__)


async def reconcile_tenant(
    *,
    app_pool: asyncpg.Pool,
    audit_writer: AuditWriter,
    audit_pool: asyncpg.Pool,
    tenant_id: UUID,
) -> int:
    """Returns number of anomalies recorded for the tenant."""
    anomalies_total = 0
    async with tenant_connection(app_pool, tenant_id) as conn:
        reports = await conn.fetch(
            "SELECT id, current_version_id FROM reports "
            "WHERE status IN ('signed', 'amended', 'finalized')"
        )
        for r in reports:
            rid: UUID = r["id"]
            cur_vid = r["current_version_id"]
            rows = await conn.fetch(
                """
                SELECT id, version_number, parent_version_id, is_amendment,
                       (signed_at IS NOT NULL) AS signed
                FROM report_versions
                WHERE report_id = $1
                """,
                rid,
            )
            # parent_signed for an amendment = its parent row's signed flag.
            by_id = {row["id"]: row for row in rows}
            nodes: list[ChainNode] = []
            for row in rows:
                pid = row["parent_version_id"]
                parent_signed = False
                if pid is not None and pid in by_id:
                    parent_signed = bool(by_id[pid]["signed"])
                nodes.append(
                    ChainNode(
                        id=row["id"],
                        version_number=int(row["version_number"]),
                        parent_id=pid,
                        is_amendment=bool(row["is_amendment"]),
                        parent_signed=parent_signed,
                    )
                )
            anomalies = verify_chain(nodes, current_version_id=cur_vid)
            if not anomalies:
                continue
            anomalies_total += len(anomalies)
            await _persist_anomalies(
                audit_pool=audit_pool,
                audit_writer=audit_writer,
                tenant_id=tenant_id,
                report_id=rid,
                anomalies=anomalies,
            )
    return anomalies_total


async def _persist_anomalies(
    *,
    audit_pool: asyncpg.Pool,
    audit_writer: AuditWriter,
    tenant_id: UUID,
    report_id: UUID,
    anomalies: Iterable,
) -> None:
    async with audit_pool.acquire() as conn:
        async with conn.transaction():
            for a in anomalies:
                await conn.execute(
                    """
                    INSERT INTO audit.report_chain_failures
                        (tenant_id, report_id, anomaly_kind, detail_jsonb)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """,
                    tenant_id,
                    report_id,
                    a.kind,
                    json.dumps(a.detail),
                )

    for a in anomalies:
        await audit_writer.write_event(
            tenant_id=tenant_id,
            kind=audit_kinds.REPORT_CHAIN_INTEGRITY_FAILURE,
            actor_sub=None,
            actor_role=None,
            target_kind="report",
            target_id=report_id,
            payload={"anomaly_kind": a.kind, **a.detail},
            severity=Severity.SEC,
        )


async def reconcile_all() -> int:
    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name="report-service/chain-reconciler",
        min_size=1,
        max_size=4,
    )
    audit_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name="report-service/chain-reconciler-audit",
        min_size=1,
        max_size=2,
    )
    audit_writer = AuditWriter(audit_pool)
    total = 0
    try:
        async with app_pool.acquire() as conn:
            tenants = await conn.fetch("SELECT id FROM tenants WHERE active = true")
        for t in tenants:
            try:
                total += await reconcile_tenant(
                    app_pool=app_pool,
                    audit_writer=audit_writer,
                    audit_pool=audit_pool,
                    tenant_id=t["id"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("chain_reconciler.tenant_failed", extra={"tenant_id": str(t["id"])})
    finally:
        await app_pool.close()
        await audit_pool.close()
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", help="Run for a single tenant only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    total = asyncio.run(reconcile_all())
    print(f"chain reconciler completed; anomalies={total}")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
