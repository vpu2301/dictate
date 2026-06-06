"""Step 8 — per-tenant monthly quota check.

Runs in the same transaction as the row insert so a TOCTOU race between
``SELECT SUM(...)`` and the subsequent INSERT can't slip past the cap.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg

from .result import ValidationCode, ValidationResult, ok, reject


async def validate_quota(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    incoming_size_bytes: int,
    monthly_quota_bytes: int,
) -> ValidationResult:
    """Sum the tenant's current-month uploads and reject on overflow.

    Caller must hold an RLS-scoped transaction (``tenant_connection``)
    so the SELECT only sees this tenant's rows and the subsequent
    INSERT is part of the same transaction (preventing TOCTOU).
    """
    row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(size_bytes), 0)::BIGINT AS used_bytes
        FROM audio_files
        WHERE created_at >= date_trunc('month', now())
          AND status <> 'deleted'
        FOR UPDATE
        """,
    )
    used = int(row["used_bytes"]) if row is not None else 0
    if used + incoming_size_bytes > monthly_quota_bytes:
        return reject(
            ValidationCode.QUOTA_EXCEEDED,
            f"tenant {tenant_id} has used {used} of {monthly_quota_bytes} "
            f"bytes this month; adding {incoming_size_bytes} would exceed.",
        )
    return ok()
