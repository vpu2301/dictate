"""Recording-consent gate for conversation sessions (sprint 14).

Mirrors ``domain/encounters.py``: a direct RLS-scoped read of the
core-service-owned tables (the repo's sanctioned pattern for
cross-service reads — one database, tenant isolation enforced by
Postgres RLS, soft references instead of FKs; see migration 0031's
header). HTTP is reserved for actions; a consent *check* is a read.

Conversation mode records the PATIENT, so it requires:
  * an encounter (checked by the existing encounter gate), and
  * a granted, non-withdrawn ``recording`` consent for that encounter's
    patient — either encounter-scoped or patient-wide
    (``encounter_id IS NULL``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ..protocol.error_catalogue import ErrorCode

CONSENT_TYPE_RECORDING = "recording"


async def fetch_recording_consent(conn: Any, *, encounter_id: UUID) -> dict[str, Any] | None:
    """Return ``{patient_id, consent_id}`` when the encounter's patient
    has a granted 'recording' consent covering this encounter, else None.

    Runs under ``db.tenant_connection`` — cross-tenant rows are
    invisible, so "no consent" and "not your tenant" are
    indistinguishable by design (no existence oracle).
    """
    row = await conn.fetchrow(
        """
        SELECT e.patient_id, c.id AS consent_id
          FROM encounters e
          JOIN patient_consents c
            ON c.patient_id = e.patient_id
           AND c.type = $2
           AND c.status = 'granted'
           AND (c.encounter_id IS NULL OR c.encounter_id = e.id)
         WHERE e.id = $1
         ORDER BY c.granted_at DESC
         LIMIT 1
        """,
        encounter_id,
        CONSENT_TYPE_RECORDING,
    )
    if row is None:
        return None
    return {"patient_id": row["patient_id"], "consent_id": row["consent_id"]}


def consent_gate(consent: dict[str, Any] | None) -> ErrorCode | None:
    """None -> proceed; CONSENT_REQUIRED -> refuse the conversation."""
    if consent is None:
        return ErrorCode.CONSENT_REQUIRED
    return None
