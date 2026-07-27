"""Encounter-linkage gate for session start (S11 step 02).

A dictation session that names an ``encounter_id`` must reference a real,
in-tenant, dictable encounter *before any audio is accepted*. The queries
run under :func:`db.tenant_connection`, so a cross-tenant encounter is
RLS-invisible and indistinguishable from a nonexistent one — no existence
oracle.

"Closed" is mode-dependent, and deliberately so.

For **dictation** (and for asr-service's batch jobs, which share this gate)
only ``cancelled`` is a closed door. Statuses are ``scheduled | in_progress
| paused | completed | cancelled`` with DEFAULT ``completed`` — encounters
are routinely recorded *after* the visit, and transcribing audio into an
already-finished encounter is the normal flow.

For **conversation** mode the rule is stricter: an ambient-scribe session
records a consultation as it happens, so the visit has to still be open.
Once the clinician ends the visit (0058) the microphone must not be able to
re-open on it — that is precisely the "active visits stuck in the pipeline"
failure this gate now prevents from recurring.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg

from ..protocol.error_catalogue import ErrorCode

#: Refused for every mode.
_CLOSED_STATUSES = frozenset({"cancelled"})
#: Live conversation capture additionally requires an *open* visit.
_CONVERSATION_OPEN_STATUSES = frozenset({"scheduled", "in_progress", "paused"})


async def fetch_encounter_status(
    conn: asyncpg.Connection, *, encounter_id: UUID
) -> str | None:
    """Status of the encounter, or None when nonexistent / cross-tenant
    (RLS makes those identical)."""
    return await conn.fetchval(
        "SELECT status FROM encounters WHERE id = $1", encounter_id
    )


def encounter_gate(status: str | None, *, mode: str = "dictation") -> ErrorCode | None:
    """Map a fetched encounter status to the protocol rejection, if any."""
    if status is None:
        return ErrorCode.ENCOUNTER_INVALID
    if status in _CLOSED_STATUSES:
        return ErrorCode.ENCOUNTER_CLOSED
    if mode == "conversation" and status not in _CONVERSATION_OPEN_STATUSES:
        return ErrorCode.ENCOUNTER_CLOSED
    return None
