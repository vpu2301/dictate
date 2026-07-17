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


async def list_patient_recordings(
    conn: asyncpg.Connection, *, patient_id: UUID, limit: int = 200
) -> list[asyncpg.Record]:
    """Encounter-linked recordings for the patient — metadata only.

    S11 step 02: the `audio_files.encounter_id` FK makes
    recording → encounter → patient a real join, so "every recording of
    this patient" is this query. Deliberately no storage_uri and no
    presigned URL — the timeline carries metadata; media access stays on
    the ASR surface with its own authz + audit."""
    return list(
        await conn.fetch(
            """
            SELECT a.id, a.encounter_id, a.duration_ms, a.status, a.created_at
            FROM audio_files a
            JOIN encounters e ON e.id = a.encounter_id
            WHERE e.patient_id = $1
              AND a.status <> 'deleted'
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT $2
            """,
            patient_id,
            limit,
        )
    )
