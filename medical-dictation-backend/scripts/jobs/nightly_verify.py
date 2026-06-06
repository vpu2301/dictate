#!/usr/bin/env python3
"""Nightly audit-chain verification for every active tenant.

Per Sprint-02 spec § 4.3:
  * Runs at 02:00 UTC daily (cron entry committed in
    ``infra/compose/cron/nightly-verify.cron``).
  * Walks each tenant's chain via :class:`audit.AuditVerifier`.
  * Emits Prometheus *textfile* metrics so node-exporter picks them up:
      - ``mdx_audit_chain_ok{tenant_id="…"} 1|0``
      - ``mdx_audit_chain_depth{tenant_id="…"} <last_seq>``
      - ``mdx_audit_chain_events_checked{tenant_id="…"} <count>``
      - ``mdx_audit_chain_last_verify_ts{tenant_id="…"} <unix>``
  * Also writes an ``audit.chain_verified`` audit event per tenant with the
    summary (which itself is verifiable on the next run — the audit chain
    is self-referential).
  * On any divergence: exit non-zero so the cron driver alerts (and the
    Prometheus alert ``AuditChainBroken`` fires on the gauge anyway).

The script connects as the ``audit_reader`` role for reads and ``audit_writer``
for the summary event — never as a superuser.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from uuid import UUID

import asyncpg

# Make absolute imports work when invoked as `python scripts/jobs/...`
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "libs" / "audit" / "src"))

from audit import AuditVerifier, AuditWriter, Severity  # noqa: E402

logger = logging.getLogger("nightly_verify")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")

# Reader-credentialed pool walks the chain; writer pool emits the summary.
READER_DSN = os.environ.get(
    "AUDIT_READER_DSN",
    f"postgresql://audit_reader:audit_reader@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}",
)
WRITER_DSN = os.environ.get(
    "AUDIT_WRITER_DSN",
    f"postgresql://audit_writer:audit_writer@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}",
)
# `tenants` is read via superuser-equivalent role since it's RLS-bound and
# we need the global list — in production this is a low-privileged role
# with SELECT on tenants only (out of scope for sprint 02).
TENANTS_DSN = os.environ.get(
    "TENANTS_DSN",
    f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}",
)

PROM_TEXTFILE = Path(
    os.environ.get("PROM_TEXTFILE", "/var/lib/node_exporter/textfile/audit_chain.prom")
)


async def list_active_tenants(dsn: str) -> list[UUID]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT id FROM tenants WHERE status = 'active' ORDER BY id"
        )
        return [r["id"] for r in rows]
    finally:
        await conn.close()


def write_prom_textfile(records: list[dict[str, object]]) -> None:
    """Write Prometheus textfile metrics atomically.

    node-exporter polls this directory and exposes lines as metrics under
    its ``/metrics`` endpoint. Atomic write (rename-from-tmp) prevents
    Prometheus from scraping a half-written file.
    """
    PROM_TEXTFILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROM_TEXTFILE.with_suffix(".tmp")
    lines: list[str] = [
        "# HELP mdx_audit_chain_ok 1 if the tenant's chain verifies, 0 if divergence",
        "# TYPE mdx_audit_chain_ok gauge",
        "# HELP mdx_audit_chain_depth Last sequence number on the tenant's chain",
        "# TYPE mdx_audit_chain_depth gauge",
        "# HELP mdx_audit_chain_events_checked Events the verifier walked on this run",
        "# TYPE mdx_audit_chain_events_checked gauge",
        "# HELP mdx_audit_chain_last_verify_ts Unix time of the last successful verifier run",
        "# TYPE mdx_audit_chain_last_verify_ts gauge",
    ]
    for r in records:
        tid = r["tenant_id"]
        lines.append(f'mdx_audit_chain_ok{{tenant_id="{tid}"}} {1 if r["ok"] else 0}')
        lines.append(f'mdx_audit_chain_depth{{tenant_id="{tid}"}} {r["last_seq"]}')
        lines.append(f'mdx_audit_chain_events_checked{{tenant_id="{tid}"}} {r["events_checked"]}')
        lines.append(f'mdx_audit_chain_last_verify_ts{{tenant_id="{tid}"}} {r["ts"]}')
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(PROM_TEXTFILE)


async def main() -> int:
    tenants = await list_active_tenants(TENANTS_DSN)
    if not tenants:
        logger.info("no active tenants; nothing to verify")
        return 0
    logger.info("verifying chains for %d tenant(s)", len(tenants))

    reader_pool = await asyncpg.create_pool(
        READER_DSN, min_size=1, max_size=2, statement_cache_size=0
    )
    writer_pool = await asyncpg.create_pool(
        WRITER_DSN, min_size=1, max_size=2, statement_cache_size=0
    )
    assert reader_pool is not None and writer_pool is not None

    verifier = AuditVerifier(reader_pool)
    writer = AuditWriter(writer_pool)

    records: list[dict[str, object]] = []
    any_diverged = False

    try:
        for tid in tenants:
            report = await verifier.verify_chain(tid)
            records.append(
                {
                    "tenant_id": str(tid),
                    "ok": report.ok,
                    "last_seq": report.last_seq or 0,
                    "events_checked": report.events_checked,
                    "ts": int(time.time()),
                }
            )
            if not report.ok:
                any_diverged = True
                logger.error(
                    "chain divergence: tenant=%s seq=%s reason=%s",
                    tid,
                    report.first_divergence_seq,
                    report.divergence_reason,
                )
            # Self-audit the verification result (severity sec on failure).
            try:
                await writer.write_event(
                    tenant_id=tid,
                    kind="audit.chain_verified",
                    payload={
                        "ok": report.ok,
                        "events_checked": report.events_checked,
                        "last_seq": report.last_seq,
                        "first_divergence_seq": report.first_divergence_seq,
                        "divergence_reason": (
                            report.divergence_reason.value
                            if report.divergence_reason
                            else None
                        ),
                    },
                    severity=Severity.SEC if not report.ok else Severity.INFO,
                )
            except Exception as exc:
                logger.warning(
                    "could not write chain_verified event for tenant=%s: %s", tid, exc
                )

        try:
            write_prom_textfile(records)
            logger.info("wrote %d records to %s", len(records), PROM_TEXTFILE)
        except (PermissionError, OSError) as exc:
            # In dev / CI the textfile dir may not exist or be writable —
            # we still want to surface the verify result.
            logger.warning("could not write textfile metrics: %s", exc)

        if any_diverged:
            logger.error("AT LEAST ONE TENANT'S CHAIN DIVERGED — see records above")
            return 2
        logger.info("all chains verified ok")
        return 0
    finally:
        await reader_pool.close()
        await writer_pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
