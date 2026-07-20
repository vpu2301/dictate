"""Contract tests for the NotificationEvent envelope."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from notification_events import Category, NotificationEvent


def _event(**overrides: object) -> NotificationEvent:
    base: dict[str, object] = {
        "event_id": uuid4(),
        "tenant_id": uuid4(),
        "category": Category.REPORT_FINALIZED,
        "actor_user_id": uuid4(),
        "resource_type": "report",
        "resource_id": uuid4(),
        "occurred_at": datetime.now(UTC),
    }
    base.update(overrides)
    return NotificationEvent(**base)  # type: ignore[arg-type]


def test_round_trips_through_json() -> None:
    event = _event(payload={"report_code": "RPT-2026-0001"})
    restored = NotificationEvent.model_validate_json(event.model_dump_json())
    assert restored == event


def test_unknown_field_is_rejected() -> None:
    """extra='forbid' — deploy skew must fail loudly, not drop data."""
    with pytest.raises(ValidationError):
        _event(patient_name="Іваненко")


def test_naive_occurred_at_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _event(occurred_at=datetime(2026, 7, 19, 12, 0))  # noqa: DTZ001


def test_nested_payload_is_rejected() -> None:
    """The flat-scalar rule is the first line of the PHI boundary."""
    with pytest.raises(ValidationError):
        _event(payload={"body": {"sections": ["diagnosis"]}})


def test_oversized_payload_value_is_rejected() -> None:
    with pytest.raises(ValidationError, match="pointers, not content"):
        _event(payload={"note": "x" * 201})


def test_too_many_payload_keys_rejected() -> None:
    with pytest.raises(ValidationError, match="max is 20"):
        _event(payload={f"k{i}": i for i in range(21)})


def test_dedupe_key_is_stable_per_event_and_recipient() -> None:
    """Replaying the same event must collapse onto the same row."""
    event = _event()
    recipient = uuid4()
    assert event.dedupe_key(recipient) == event.dedupe_key(recipient)

    replay = _event(
        event_id=event.event_id,
        tenant_id=event.tenant_id,
        resource_id=event.resource_id,
        occurred_at=event.occurred_at,
    )
    assert replay.dedupe_key(recipient) == event.dedupe_key(recipient)


def test_dedupe_key_differs_per_recipient() -> None:
    """One fact fans out to N rows — they must not collide."""
    event = _event()
    assert event.dedupe_key(uuid4()) != event.dedupe_key(uuid4())


def test_event_is_frozen() -> None:
    event = _event()
    with pytest.raises(ValidationError):
        event.tenant_id = uuid4()  # type: ignore[misc]
