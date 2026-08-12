"""Reading a Google appointment-schedule invitation.

The fixture is the shape Google actually sends, including the 75-octet
line folding that broke the first version of the parser.
"""

from __future__ import annotations

from datetime import UTC, datetime

from marketing_service.domain.ics import parse_invite, unfold

GOOGLE_INVITE = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "PRODID:-//Google Inc//Google Calendar 70.9054//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "DTSTART;TZID=Europe/Kyiv:20260820T150000",
        "DTEND;TZID=Europe/Kyiv:20260820T154000",
        "DTSTAMP:20260809T112400Z",
        "ORGANIZER;CN=Klarnote Sales:mailto:sales@klarnote.com",
        "UID:6f2c1d9e4b7a@google.com",
        "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;",
        " CN=Olena K;X-NUM-GUESTS=0:mailto:Olena.K@orion-med.ua",
        "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;",
        " CN=Klarnote Sales;X-NUM-GUESTS=0:mailto:sales@klarnote.com",
        "X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij",
        "CREATED:20260809T112400Z",
        "DESCRIPTION:Klarnote product demo",
        "LAST-MODIFIED:20260809T112400Z",
        "LOCATION:",
        "SEQUENCE:0",
        "STATUS:CONFIRMED",
        "SUMMARY:Klarnote — product demo",
        "TRANSP:OPAQUE",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
)


def test_unfold_rejoins_continuation_lines() -> None:
    folded = "SUMMARY:Klarnote — pro\r\n duct demo"
    assert unfold(folded) == ["SUMMARY:Klarnote — product demo"]


def test_parses_the_essentials() -> None:
    invite = parse_invite(GOOGLE_INVITE)
    assert invite is not None
    assert invite.uid == "6f2c1d9e4b7a@google.com"
    assert invite.sequence == 0
    assert invite.summary == "Klarnote — product demo"
    assert invite.meet_url == "https://meet.google.com/abc-defg-hij"
    assert invite.timezone_name == "Europe/Kyiv"
    assert not invite.cancelled


def test_local_time_is_anchored_to_the_named_zone() -> None:
    """15:00 Kyiv is 12:00 UTC in August — not 15:00 UTC.

    Getting this wrong shifts every confirmation by the offset and the
    mistake only surfaces when somebody misses a call.
    """
    invite = parse_invite(GOOGLE_INVITE)
    assert invite is not None
    assert invite.starts_at.astimezone(UTC) == datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    assert invite.ends_at.astimezone(UTC) == datetime(2026, 8, 20, 12, 40, tzinfo=UTC)


def test_attendees_are_lowercased_and_folded_ones_survive() -> None:
    invite = parse_invite(GOOGLE_INVITE)
    assert invite is not None
    assert invite.attendees == ("olena.k@orion-med.ua", "sales@klarnote.com")
    assert invite.organizer == "sales@klarnote.com"


def test_utc_dtstart_form() -> None:
    raw = GOOGLE_INVITE.replace(
        "DTSTART;TZID=Europe/Kyiv:20260820T150000", "DTSTART:20260820T120000Z"
    ).replace("DTEND;TZID=Europe/Kyiv:20260820T154000", "DTEND:20260820T124000Z")
    invite = parse_invite(raw)
    assert invite is not None
    assert invite.starts_at == datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    assert invite.timezone_name == ""


def test_missing_dtend_falls_back_to_the_configured_length() -> None:
    raw = GOOGLE_INVITE.replace("DTEND;TZID=Europe/Kyiv:20260820T154000\r\n", "")
    invite = parse_invite(raw, default_minutes=40)
    assert invite is not None
    assert (invite.ends_at - invite.starts_at).total_seconds() == 40 * 60


def test_duration_is_used_when_present() -> None:
    raw = GOOGLE_INVITE.replace(
        "DTEND;TZID=Europe/Kyiv:20260820T154000", "DURATION:PT25M"
    )
    invite = parse_invite(raw)
    assert invite is not None
    assert (invite.ends_at - invite.starts_at).total_seconds() == 25 * 60


def test_cancellation_is_recognised_from_method_and_from_status() -> None:
    by_method = parse_invite(GOOGLE_INVITE.replace("METHOD:REQUEST", "METHOD:CANCEL"))
    by_status = parse_invite(
        GOOGLE_INVITE.replace("STATUS:CONFIRMED", "STATUS:CANCELLED")
    )
    assert by_method is not None and by_method.cancelled
    assert by_status is not None and by_status.cancelled


def test_reschedule_bumps_the_sequence() -> None:
    """The (uid, sequence) pair is what lets a reschedule re-send."""
    invite = parse_invite(GOOGLE_INVITE.replace("SEQUENCE:0", "SEQUENCE:2"))
    assert invite is not None
    assert invite.sequence == 2


def test_meet_url_recovered_from_the_body_when_the_header_is_absent() -> None:
    raw = GOOGLE_INVITE.replace(
        "X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij", "LOCATION:x"
    ).replace(
        "DESCRIPTION:Klarnote product demo",
        "DESCRIPTION:Join at https://meet.google.com/abc-defg-hij please",
    )
    invite = parse_invite(raw)
    assert invite is not None
    assert invite.meet_url == "https://meet.google.com/abc-defg-hij"


def test_garbage_returns_none_rather_than_raising() -> None:
    assert parse_invite("") is None
    assert parse_invite("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n") is None
    # A VEVENT with no UID is unusable — we could not deduplicate it.
    assert parse_invite("BEGIN:VEVENT\r\nDTSTART:20260820T120000Z\r\nEND:VEVENT") is None
