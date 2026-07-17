"""Encounter-linkage gate for session start (S11 step 02).

A dictation session that names an ``encounter_id`` must reference a real,
in-tenant, dictable encounter *before any audio is accepted*. The queries
run under :func:`db.tenant_connection`, so a cross-tenant encounter is
RLS-invisible and indistinguishable from a nonexistent one — no existence
oracle.

"Closed" maps to the as-built encounter model (0031): statuses are
``scheduled | in_progress | completed | cancelled`` with DEFAULT
``completed`` — encounters are routinely recorded after the visit, and the
SPA creates them without a status. Dictating into a ``completed``
encounter is therefore the *normal* flow; only ``cancelled`` is a closed
door.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg

from ..protocol.error_catalogue import ErrorCode

_CLOSED_STATUSES = frozenset({"cancelled"})


async def fetch_encounter_status(
    conn: asyncpg.Connection, *, encounter_id: UUID
) -> str | None:
    """Status of the encounter, or None when nonexistent / cross-tenant
    (RLS makes those identical)."""
    return await conn.fetchval(
        "SELECT status FROM encounters WHERE id = $1", encounter_id
    )


def encounter_gate(status: str | None) -> ErrorCode | None:
    """Map a fetched encounter status to the protocol rejection, if any."""
    if status is None:
        return ErrorCode.ENCOUNTER_INVALID
    if status in _CLOSED_STATUSES:
        return ErrorCode.ENCOUNTER_CLOSED
    return None
