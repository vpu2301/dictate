"""Read-only projection of a patient's reports for the unified timeline.

``reports`` is owned by report-service, but it lives in the same database and
already carries a (soft) ``patient_id`` (migration 0016). The patient card's
timeline + Reports tab are fed entirely from here, so core-service performs a
read-only, RLS-scoped SELECT against ``reports`` rather than a cross-service
HTTP round-trip. This is reads-only and tenant-isolated by the same
``app.tenant_id`` RLS predicate report-service relies on — no write path and
no schema ownership is taken.

Scribe sessions (``kind='scribe'``) will be added here once that table lands;
today the timeline surfaces dictated reports only.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg


async def list_patient_reports(
    conn: asyncpg.Connection, *, patient_id: UUID, limit: int = 200
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            """
            SELECT id, title, code, status,
                   encounter_date, created_at, updated_at
            FROM reports
            WHERE patient_id = $1
            ORDER BY COALESCE(updated_at, created_at) DESC, id DESC
            LIMIT $2
            """,
            patient_id,
            limit,
        )
    )
