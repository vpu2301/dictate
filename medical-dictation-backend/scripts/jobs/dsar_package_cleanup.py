"""DSAR package TTL cleanup (S11 step 06) — cron, daily.

Deletes DSAR ZIP objects older than ``DSAR_PACKAGE_TTL_DAYS`` (default 14)
and stamps ``package_deleted_at`` on the request row (the download
endpoint answers 410 ``package_expired`` afterwards). Runs with an
operational DSN (cross-tenant by design — the sweep is the whole point),
mirroring the nightly-verify cron precedent; compose stays infra-only.

    uv run python scripts/jobs/dsar_package_cleanup.py

Idempotent: object deletion tolerates absence; only rows still carrying a
key are considered.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, "libs/storage/src")
from storage import S3Client  # noqa: E402  (ciphertext deletion only — no envelope needed)

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/medical_dictation"


async def main() -> int:
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)
    ttl_days = int(os.environ.get("DSAR_PACKAGE_TTL_DAYS", "14"))
    bucket = os.environ.get("S3_DSAR_BUCKET", "mdx-dsar")
    s3 = S3Client(
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://localhost:9000"),
        access_key=os.environ.get("S3_ACCESS_KEY", "minioadmin"),
        secret_key=os.environ.get("S3_SECRET_KEY", "minioadmin"),
        region=os.environ.get("S3_REGION", "us-east-1"),
        use_ssl=os.environ.get("S3_USE_SSL", "false").lower() == "true",
    )

    conn = await asyncpg.connect(dsn)
    deleted = 0
    try:
        rows = await conn.fetch(
            """
            SELECT id, package_object_key FROM patient_privacy_requests
            WHERE kind = 'dsar' AND package_object_key IS NOT NULL
              AND package_deleted_at IS NULL
              AND completed_at < now() - make_interval(days => $1)
            """,
            ttl_days,
        )
        for row in rows:
            await s3.delete_object(bucket=bucket, key=row["package_object_key"])
            await conn.execute(
                "UPDATE patient_privacy_requests SET package_deleted_at = now() "
                "WHERE id = $1",
                row["id"],
            )
            deleted += 1
            print(f"deleted: request={row['id']} key={row['package_object_key']}")
    finally:
        await conn.close()
        await s3.aclose()

    print(f"ok: dsar-package-cleanup removed {deleted} expired package(s) (ttl={ttl_days}d)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
