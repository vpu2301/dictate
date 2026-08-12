"""Formatted values and the calendar file — the parts a mail client reads."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from marketing_service.domain import compose, copy

KYIV = ZoneInfo("Europe/Kyiv")
START = datetime(2026, 9, 3, 15, 0, tzinfo=KYIV)
END = datetime(2026, 9, 3, 15, 40, tzinfo=KYIV)


def test_dates_read_naturally_in_each_language() -> None:
    assert copy.long_date(START, "en") == "Thursday, 3 September 2026"
    assert copy.long_date(START, "de") == "Donnerstag, 3. September 2026"
    # Genitive month, as a Ukrainian speaker would write it.
    assert copy.long_date(START, "uk") == "четвер, 3 вересня 2026"


def test_time_range_names_the_city_and_the_offset() -> None:
    assert copy.time_range(START, END, "Europe/Kyiv") == "15:00 – 15:40 Kyiv (UTC+3)"


def test_confirmation_renders_the_slot_in_the_event_zone_not_utc() -> None:
    """The stored time is absolute; the mail must say 15:00, not 12:00."""
    fields = compose.demo_confirmed_fields(
        lang="en",
        first_name=None,
        token="t",
        starts_at=START.astimezone(UTC),
        ends_at=END.astimezone(UTC),
        timezone_name="Europe/Kyiv",
        meet_url="https://meet.google.com/x",
        booking_url="https://calendar.app.google/X",
        public_base_url="https://klarnote.com",
        host_name="Host",
        reply_to="sales@klarnote.com",
        demo_minutes=40,
    )
    assert fields["time_label"] == "15:00 – 15:40 Kyiv (UTC+3)"
    assert fields["date_label"] == "Thursday, 3 September 2026"
    assert fields["duration_label"] == "40 minutes"


def test_add_to_calendar_url_is_stamped_in_utc() -> None:
    url = compose.add_to_calendar_url(
        start=START, end=END, meet_url="https://meet.google.com/x"
    )
    # 15:00 Kyiv in September is 12:00 UTC. A prefill without the Z would
    # land the event at the reader's own 15:00.
    assert "dates=20260903T120000Z%2F20260903T124000Z" in url


def test_greeting_falls_back_to_a_real_greeting_not_a_blank() -> None:
    assert copy.greeting(None, "de") == "Guten Tag,"
    assert copy.greeting("Olena", "uk") == "Доброго дня, Olena,"


def test_unsubscribe_and_privacy_are_built_from_the_public_origin() -> None:
    assert compose.unsubscribe_url(
        public_base_url="https://klarnote.com/", token="a b"
    ) == "https://klarnote.com/unsubscribe?t=a%20b"
    assert (
        compose.privacy_url(public_base_url="https://klarnote.com")
        == "https://klarnote.com/legal/privacy"
    )


def test_cancel_is_a_mailto_in_the_readers_language() -> None:
    url = compose.cancel_url(reply_to="sales@klarnote.com", lang="de")
    assert url.startswith("mailto:sales@klarnote.com?")
    assert "Klarnote-Demo+absagen" in url


# ── the .ics ─────────────────────────────────────────────────────────
def build() -> str:
    return compose.build_ics(
        uid="6f2c@google.com",
        starts_at=START,
        ends_at=END,
        meet_url="https://meet.google.com/abc-defg-hij",
        organizer="sales@klarnote.com",
        stamp=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
    )


def test_ics_is_a_publish_not_a_request() -> None:
    """REQUEST would duplicate the event Google already invited them to."""
    assert "METHOD:PUBLISH" in build()
    assert "METHOD:REQUEST" not in build()


def test_ics_uses_crlf_and_utc_stamps() -> None:
    body = build()
    assert body.endswith("\r\n")
    assert "\n" not in body.replace("\r\n", "")
    assert "DTSTART:20260903T120000Z" in body
    assert "DTEND:20260903T124000Z" in body


def test_ics_folds_long_lines_without_splitting_a_codepoint() -> None:
    body = compose.build_ics(
        uid="x@y",
        starts_at=START,
        ends_at=END,
        meet_url="",
        organizer="",
        stamp=START,
        summary="Кларноут — демонстрація продукту для великої клініки в Києві",
    )
    for line in body.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    # Round-trips: unfolding restores the summary intact.
    from marketing_service.domain.ics import unfold

    joined = "\n".join(unfold(body))
    assert "Кларноут — демонстрація продукту для великої клініки в Києві" in joined


def test_ics_escapes_separators_that_would_break_the_grammar() -> None:
    body = compose.build_ics(
        uid="x@y",
        starts_at=START,
        ends_at=END,
        meet_url="",
        organizer="",
        stamp=START,
        summary="Demo; with, separators",
    )
    assert "SUMMARY:Demo\\; with\\, separators" in body


def test_host_name_is_transliterated_for_ukrainian() -> None:
    """A Ukrainian letter signed in Latin letters reads as a translation."""
    assert copy.host_name_for("Andrii Kovalchuk", "uk") == "Андрій Ковальчук"
    assert copy.host_name_for("Andrii Kovalchuk", "de") == "Andrii Kovalchuk"
    # An unknown name passes through rather than disappearing.
    assert copy.host_name_for("Someone Else", "uk") == "Someone Else"


def test_receipt_stamp_is_shown_in_the_readers_wall_clock() -> None:
    """Stored UTC; a Kyiv reader who wrote at 19:45 must not read 16:45."""
    submitted = datetime(2026, 8, 9, 16, 45, tzinfo=UTC)
    assert copy.short_datetime(submitted, "uk") == "9 серпня 2026, 19:45"
    assert copy.short_datetime(submitted, "de") == "9. August 2026, 18:45"
