"""Patient-kind break-glass grants (S15) — RLS-scoped SQL over
``phi_access_requests`` (migrations 0056 + 0061).

The core-service twin of report-service's ``phi_access_repository``:
grants are MINTED there (``POST /v1/phi-access-requests`` is the single
door for both kinds), but a patient-kind grant is ENFORCED here, where
the patient record lives. Only the lookup and the use-stamp are needed
on this side.

Every function takes a connection already bound to ``app.tenant_id`` via
:func:`db.tenant_connection` — Postgres RLS is the isolation, per
ADR-0006.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg


async def find_live_patient_grant(
    conn: asyncpg.Connection, *, user_sub: UUID, patient_id: UUID
) -> asyncpg.Record | None:
    """Does this user hold an unexpired, unrevoked grant on this patient
    right now?

    Ordered by ``expires_at DESC`` so a re-request that widens the window
    wins over an older grant about to lapse — same reasoning as the
    report-side lookup.
    """
    return await conn.fetchrow(
        """
        SELECT id, reason_code, expires_at
          FROM phi_access_requests
         WHERE requested_by  = $1
           AND resource_kind = 'patient'
           AND resource_id   = $2
           AND status        = 'granted'
           AND expires_at    > now()
         ORDER BY expires_at DESC
         LIMIT 1
        """,
        user_sub,
        patient_id,
    )


async def record_grant_use(conn: asyncpg.Connection, *, grant_id: UUID) -> None:
    """Stamp a read against its grant — every read served under
    break-glass must also be counted (see the oversight list)."""
    await conn.execute(
        """
        UPDATE phi_access_requests
           SET use_count = use_count + 1,
               last_used_at = now()
         WHERE id = $1
        """,
        grant_id,
    )
