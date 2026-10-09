"""Rendering is the PHI boundary — these assert the negative.

The blocking CI gate (`check-notification-phi-free.py`) covers the EMAIL
templates. It cannot cover `dictation.completed`, which has no template
by design, so the in-app rendering path needs its own guard here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from notification_events import Category, NotificationEvent
from notification_service.domain.render import (
    ALLOWED_PAYLOAD_KEYS,
    deep_link,
    render_body,
    render_title,
    safe_payload,
)

TENANT = uuid4()
SESSION = uuid4()
USER = uuid4()


def _dictation_event(**payload: object) -> NotificationEvent:
    return NotificationEvent(
        event_id=uuid4(),
        tenant_id=TENANT,
        category=Category.DICTATION_COMPLETED,
        actor_user_id=USER,
        resource_type="dictation_session",
        resource_id=SESSION,
        occurred_at=datetime.now(UTC),
        recipient_hints=(USER,),
        payload=payload,  # type: ignore[arg-type]
    )


def test_transcript_text_never_survives_into_a_rendered_row() -> None:
    """A producer that adds transcript text finds it silently dropped.

    The transcript IS the PHI for a dictation. This is the whole reason
    the boundary is an allow-list rather than a scrubber: no pattern
    match would recognise a Ukrainian clinical narrative as sensitive.
    """
    event = _dictation_event(
        duration_ms=42_000,
        segments=7,
        transcript="Пацієнт Іваненко, цукровий діабет, E11.9",
        patient_name="Іваненко",
    )

    fields = safe_payload(event)

    assert fields == {"duration_ms": "42000", "segments": "7"}
    for leak in ("Іваненко", "цукровий діабет", "E11.9"):
        assert leak not in render_title(event)
        assert leak not in render_body(event)
        assert leak not in str(fields)


def test_dictation_payload_allow_list_is_counts_only() -> None:
    assert ALLOWED_PAYLOAD_KEYS[Category.DICTATION_COMPLETED] == frozenset(
        {"duration_ms", "segments"}
    )


def test_dictation_deep_link_points_at_the_session() -> None:
    event = _dictation_event(duration_ms=1_000, segments=1)
    assert deep_link(event, base_url="https://app.example/") == (
        f"https://app.example/dictations/{SESSION}"
    )


def test_transcription_failure_never_renders_the_error_detail() -> None:
    """`error_kind` is a closed vocabulary; `error_detail` is free text.

    A worker exception that quotes the audio or the partial transcript it
    choked on would otherwise put PHI straight into the feed.
    """
    event = NotificationEvent(
        event_id=uuid4(),
        tenant_id=TENANT,
        category=Category.TRANSCRIPTION_FAILED,
        actor_user_id=USER,
        resource_type="transcription_job",
        resource_id=SESSION,
        occurred_at=datetime.now(UTC),
        recipient_hints=(USER,),
        payload={
            "error_kind": "corrupt_audio",
            "error_detail": "failed decoding Іваненко_1978-04-12.wav",
        },
    )

    assert safe_payload(event) == {"error_kind": "corrupt_audio"}
    assert "Іваненко" not in render_body(event)
    assert "1978-04-12" not in render_body(event)
    assert "corrupt_audio" in render_body(event)


def test_transcription_completion_never_renders_a_filename() -> None:
    """An upload named after the patient is the obvious leak here."""
    event = NotificationEvent(
        event_id=uuid4(),
        tenant_id=TENANT,
        category=Category.TRANSCRIPTION_COMPLETED,
        actor_user_id=USER,
        resource_type="transcription_job",
        resource_id=SESSION,
        occurred_at=datetime.now(UTC),
        recipient_hints=(USER,),
        payload={
            "segments": 12,
            "duration_ms": 90_000,
            "language": "uk",
            "model": "large-v3",
            "filename": "Іваненко_1978-04-12.wav",
        },
    )

    assert "filename" not in safe_payload(event)
    assert "Іваненко" not in render_title(event)
    assert "Іваненко" not in render_body(event)


@pytest.mark.parametrize("category", list(Category))
def test_every_category_renders_without_raising(category: Category) -> None:
    """`render_title`/`render_body` match on the enum and raise on a miss.

    An enum member added without its two `case` arms would blow up inside
    the consumer, at which point the event retries into the DLQ.
    """
    event = NotificationEvent(
        event_id=uuid4(),
        tenant_id=TENANT,
        category=category,
        actor_user_id=USER,
        resource_type="report",
        resource_id=uuid4(),
        occurred_at=datetime.now(UTC),
        recipient_hints=(USER,),
        payload={},
    )
    assert render_title(event)
    assert render_body(event)


# ── S21: the MFA reminder ──────────────────────────────────────────────


def _reminder_event(**payload: object) -> NotificationEvent:
    return NotificationEvent(
        event_id=uuid4(),
        tenant_id=TENANT,
        category=Category.SECURITY_MFA_REMINDER,
        actor_user_id=uuid4(),      # the reviewer
        resource_type="user",
        resource_id=USER,           # the subject
        occurred_at=datetime.now(UTC),
        recipient_hints=(USER,),
        payload=payload,  # type: ignore[arg-type]
    )


def test_the_reminder_names_a_role_and_never_a_person() -> None:
    """Who filed an access-review finding is between them and the log.

    A producer that helpfully adds the reviewer's name finds it dropped:
    naming them turns a security ask into an interpersonal one, and the
    row is re-read later by the digest renderer.
    """
    event = _reminder_event(
        requested_by_role="auditor",
        reminder_count=2,
        requested_by_display="Оксана Аудитор",
        subject_email="likar@clinic.example",
    )

    fields = safe_payload(event)
    assert fields == {"requested_by_role": "auditor", "reminder_count": "2"}
    for leak in ("Оксана", "likar@clinic.example"):
        assert leak not in render_title(event)
        assert leak not in render_body(event)


def test_a_repeat_ask_reads_as_a_repeat_ask() -> None:
    once = render_body(_reminder_event(requested_by_role="auditor", reminder_count=1))
    twice = render_body(_reminder_event(requested_by_role="auditor", reminder_count=3))
    assert "повторно" not in once
    assert "повторно" in twice


def test_an_unknown_requester_role_degrades_to_prose_not_to_a_db_value() -> None:
    body = render_body(_reminder_event(requested_by_role="root", reminder_count=1))
    assert "root" not in body
    assert "Служба безпеки" in body


def test_the_reminder_links_to_enrolment_and_carries_no_subject_id() -> None:
    """The link must be actionable by the recipient and nobody else.

    A path with the subject's `sub` in it would read as a link that acts
    on that account; enrolment always acts on whoever is signed in.
    """
    link = deep_link(_reminder_event(requested_by_role="auditor"), base_url="https://app.example/")
    assert link == "https://app.example/mfa"
    assert str(USER) not in link
