"""Privacy-requests repository — RLS-scoped SQL over
``patient_privacy_requests`` (DSAR + the two-person erasure workflow).

State machines (DB CHECK ``privacy_status_check`` is the backstop):

    dsar:    requested → executing → completed | failed
    erasure: requested → review → approved → executing → completed
             requested | review | approved → rejected

Transitions are guarded UPDATEs (``WHERE status IN (…)``) returning the
row — a ``None`` result means the transition was illegal for the current
state (the router answers 409 ``invalid_transition``) or the id is
RLS-invisible (404). The two-person rule is enforced in the router
(explicit 403) AND by the ``privacy_two_person`` CHECK.

``mark_executing`` / ``mark_completed`` / ``mark_failed`` are the
engine-only transitions (S11 steps 06/07) — deliberately not exposed
over HTTP.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

import asyncpg

_COLUMNS = """
    id, tenant_id, patient_id, kind, reason, status,
    requested_by, requested_at, scheduled_for,
    reviewed_by, reviewed_at, rejection_reason,
    executing_at, completed_at, report_of_execution,
    package_object_key, package_deleted_at
"""


async def create_request(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    patient_id: UUID,
    requested_by: UUID,
    kind: str,
    reason: str,
    status: str,
    scheduled_for: datetime | None,
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO patient_privacy_requests
            (tenant_id, patient_id, kind, reason, status, requested_by, scheduled_for)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING {_COLUMNS}
        """,
        tenant_id,
        patient_id,
        kind,
        reason,
        status,
        requested_by,
        scheduled_for,
    )


async def get_request(
    conn: asyncpg.Connection, *, request_id: UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM patient_privacy_requests WHERE id = $1",
        request_id,
    )


async def list_requests(
    conn: asyncpg.Connection,
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[asyncpg.Record]:
    where: list[str] = []
    args: list[object] = []
    if status:
        args.append(status)
        where.append(f"status = ${len(args)}")
    if kind:
        args.append(kind)
        where.append(f"kind = ${len(args)}")
    args.append(limit)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return list(
        await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM patient_privacy_requests
            {where_sql}
            ORDER BY requested_at DESC
            LIMIT ${len(args)}
            """,
            *args,
        )
    )


async def list_for_patient(
    conn: asyncpg.Connection, *, patient_id: UUID
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM patient_privacy_requests
            WHERE patient_id = $1
            ORDER BY requested_at DESC
            """,
            patient_id,
        )
    )


# ── HTTP-exposed transitions ─────────────────────────────────────────


async def mark_review(
    conn: asyncpg.Connection, *, request_id: UUID
) -> asyncpg.Record | None:
    """'requested' → 'review' (erasure only). A pure "seen by an admin"
    marker — approval is accepted from either state; the real gates are
    the ``privacy.approve`` permission and the two-person rule."""
    return await conn.fetchrow(
        f"""
        UPDATE patient_privacy_requests
        SET status = 'review'
        WHERE id = $1 AND kind = 'erasure' AND status = 'requested'
        RETURNING {_COLUMNS}
        """,
        request_id,
    )


async def approve(
    conn: asyncpg.Connection,
    *,
    request_id: UUID,
    reviewer: UUID,
    scheduled_for: datetime,
) -> asyncpg.Record | None:
    """'requested' | 'review' → 'approved'; stamps the reviewer and the
    grace-period target the engine must respect."""
    return await conn.fetchrow(
        f"""
        UPDATE patient_privacy_requests
        SET status = 'approved', reviewed_by = $2, reviewed_at = now(),
            scheduled_for = $3
        WHERE id = $1 AND kind = 'erasure' AND status IN ('requested', 'review')
        RETURNING {_COLUMNS}
        """,
        request_id,
        reviewer,
        scheduled_for,
    )


async def reject(
    conn: asyncpg.Connection,
    *,
    request_id: UUID,
    reviewer: UUID,
    rejection_reason: str,
) -> asyncpg.Record | None:
    """'requested' | 'review' | 'approved' → 'rejected'. Rejecting an
    approved request during grace is the cancel path — cheaper than any
    undelete."""
    return await conn.fetchrow(
        f"""
        UPDATE patient_privacy_requests
        SET status = 'rejected', reviewed_by = $2, reviewed_at = now(),
            rejection_reason = $3, scheduled_for = NULL
        WHERE id = $1 AND kind = 'erasure'
          AND status IN ('requested', 'review', 'approved')
        RETURNING {_COLUMNS}
        """,
        request_id,
        reviewer,
        rejection_reason,
    )


# ── DSAR engine support (step 06) ────────────────────────────────────


async def find_active_dsar(
    conn: asyncpg.Connection, *, patient_id: UUID
) -> asyncpg.Record | None:
    """The patient's in-flight DSAR (requested/executing), if any."""
    return await conn.fetchrow(
        f"""
        SELECT {_COLUMNS} FROM patient_privacy_requests
        WHERE patient_id = $1 AND kind = 'dsar'
          AND status IN ('requested', 'executing')
        ORDER BY requested_at DESC LIMIT 1
        """,
        patient_id,
    )


async def revert_stale_executing(
    conn: asyncpg.Connection, *, request_id: UUID, stale_before: datetime
) -> asyncpg.Record | None:
    """Take over a DSAR whose worker died mid-build: 'executing' older
    than the staleness cutoff reverts to 'requested' so the caller can
    re-run it (idempotent — the package is rebuilt, the object replaced).
    On-request recovery, not on-startup: tenants are RLS-invisible to
    app_role, so a cross-tenant startup scan is impossible by design."""
    return await conn.fetchrow(
        f"""
        UPDATE patient_privacy_requests
        SET status = 'requested', executing_at = NULL
        WHERE id = $1 AND kind = 'dsar' AND status = 'executing'
          AND executing_at < $2
        RETURNING {_COLUMNS}
        """,
        request_id,
        stale_before,
    )


async def set_package_key(
    conn: asyncpg.Connection, *, request_id: UUID, key: str
) -> None:
    await conn.execute(
        "UPDATE patient_privacy_requests SET package_object_key = $2, "
        "package_deleted_at = NULL WHERE id = $1",
        request_id,
        key,
    )


# ── Engine-only transitions (steps 06/07; never HTTP) ────────────────


async def mark_executing(
    conn: asyncpg.Connection, *, request_id: UUID
) -> asyncpg.Record | None:
    """Erasure: 'approved' → 'executing', ONLY once the grace period has
    elapsed (data-layer enforcement of ``scheduled_for``). DSAR:
    'requested' → 'executing'."""
    return await conn.fetchrow(
        f"""
        UPDATE patient_privacy_requests
        SET status = 'executing', executing_at = now()
        WHERE id = $1
          AND ((kind = 'erasure' AND status = 'approved'
                AND scheduled_for IS NOT NULL AND scheduled_for <= now())
            OR (kind = 'dsar' AND status = 'requested'))
        RETURNING {_COLUMNS}
        """,
        request_id,
    )


async def mark_completed(
    conn: asyncpg.Connection,
    *,
    request_id: UUID,
    report_of_execution: dict | None = None,
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"""
        UPDATE patient_privacy_requests
        SET status = 'completed', completed_at = now(),
            report_of_execution = $2::jsonb
        WHERE id = $1 AND status = 'executing'
        RETURNING {_COLUMNS}
        """,
        request_id,
        json.dumps(report_of_execution) if report_of_execution is not None else None,
    )


async def mark_failed(
    conn: asyncpg.Connection, *, request_id: UUID
) -> asyncpg.Record | None:
    """DSAR only — a failed erasure execution stays 'executing' for retry
    (the erasure CHECK has no 'failed' state by design)."""
    return await conn.fetchrow(
        f"""
        UPDATE patient_privacy_requests
        SET status = 'failed'
        WHERE id = $1 AND kind = 'dsar' AND status = 'executing'
        RETURNING {_COLUMNS}
        """,
        request_id,
    )
