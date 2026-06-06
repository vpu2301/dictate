"""Read-only admin tool for investigating a report-chain anomaly.

Sprint-08 day-8 ships investigation tooling only; actual repair is
manual + multi-sign-off (DBA + tech lead + security lead). The script
prints the relevant chain in a way that's easy to paste into the
incident report.

Usage:
    uv run python scripts/admin/report_chain_repair.py --report-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from uuid import UUID

import asyncpg


async def dump(dsn: str, report_id: UUID) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        meta = await conn.fetchrow(
            "SELECT tenant_id, code, status, current_version_id FROM reports WHERE id = $1",
            report_id,
        )
        if meta is None:
            print(f"report {report_id} not found", file=sys.stderr)
            return 2
        versions = await conn.fetch(
            """
            SELECT id, version_number, parent_version_id, is_amendment,
                   amendment_type, amendment_reason, signed_at IS NOT NULL AS signed,
                   created_at
            FROM report_versions
            WHERE report_id = $1
            ORDER BY version_number
            """,
            report_id,
        )
        failures = await conn.fetch(
            """
            SELECT detected_at, anomaly_kind, detail_jsonb, resolved_at
            FROM audit.report_chain_failures
            WHERE report_id = $1
            ORDER BY detected_at
            """,
            report_id,
        )
        print(json.dumps(
            {
                "report": {
                    "id": str(report_id),
                    "tenant_id": str(meta["tenant_id"]),
                    "code": meta["code"],
                    "status": meta["status"],
                    "current_version_id": str(meta["current_version_id"]) if meta["current_version_id"] else None,
                },
                "versions": [
                    {
                        "id": str(v["id"]),
                        "version_number": int(v["version_number"]),
                        "parent_version_id": str(v["parent_version_id"]) if v["parent_version_id"] else None,
                        "is_amendment": bool(v["is_amendment"]),
                        "amendment_type": v["amendment_type"],
                        "amendment_reason": v["amendment_reason"],
                        "signed": bool(v["signed"]),
                        "created_at": v["created_at"].isoformat(),
                    }
                    for v in versions
                ],
                "failures": [
                    {
                        "detected_at": f["detected_at"].isoformat(),
                        "anomaly_kind": f["anomaly_kind"],
                        "detail": json.loads(f["detail_jsonb"]) if isinstance(f["detail_jsonb"], str) else f["detail_jsonb"],
                        "resolved_at": f["resolved_at"].isoformat() if f["resolved_at"] else None,
                    }
                    for f in failures
                ],
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-id", required=True, type=UUID)
    parser.add_argument(
        "--dsn",
        default=os.environ.get(
            "DB_APP_ROLE_DSN",
            "postgresql://app_role:app_role@localhost:5432/medical_dictation",
        ),
    )
    args = parser.parse_args()
    return asyncio.run(dump(args.dsn, args.report_id))


if __name__ == "__main__":
    sys.exit(main())
