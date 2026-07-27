"""Sprint-14 recording-consent gate (pure logic + SQL shape).

The DB-backed half (RLS invisibility, patient-wide vs encounter-scoped
rows) is proven live; here we pin the gate's decision table and the
query's load-bearing shape — the consent TYPE, the granted-only filter,
and the encounter parameter — so a refactor can't silently widen it.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from dictation_service.domain.consents import (
    CONSENT_TYPE_RECORDING,
    consent_gate,
    fetch_recording_consent,
)
from dictation_service.protocol.error_catalogue import ErrorCode


class _FakeConn:
    """Records the query it was asked to run; returns a canned row."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((sql, args))
        return self._row


def test_missing_consent_refuses_the_conversation() -> None:
    assert consent_gate(None) is ErrorCode.CONSENT_REQUIRED


def test_granted_consent_opens_the_gate() -> None:
    assert consent_gate({"patient_id": uuid4(), "consent_id": uuid4()}) is None


async def test_fetch_returns_patient_and_consent_ids() -> None:
    patient_id, consent_id, encounter_id = uuid4(), uuid4(), uuid4()
    conn = _FakeConn({"patient_id": patient_id, "consent_id": consent_id})

    found = await fetch_recording_consent(conn, encounter_id=encounter_id)

    assert found == {"patient_id": patient_id, "consent_id": consent_id}
    assert consent_gate(found) is None


async def test_fetch_query_shape_is_recording_and_granted_only() -> None:
    encounter_id = uuid4()
    conn = _FakeConn({"patient_id": uuid4(), "consent_id": uuid4()})

    await fetch_recording_consent(conn, encounter_id=encounter_id)

    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "patient_consents" in sql
    assert "status = 'granted'" in sql
    # The consent TYPE is bound, never interpolated — and it is the
    # recording consent, not any other kind the patient may have signed.
    assert args[0] == encounter_id  # encounter first
    assert args[1] == "recording" == CONSENT_TYPE_RECORDING


async def test_fetch_returns_none_when_no_row() -> None:
    conn = _FakeConn(None)

    found = await fetch_recording_consent(conn, encounter_id=uuid4())

    assert found is None
    assert consent_gate(found) is ErrorCode.CONSENT_REQUIRED
