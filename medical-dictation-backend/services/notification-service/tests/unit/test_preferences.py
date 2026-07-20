"""Preference + quiet-hours resolution, against a frozen clock.

Covers E8 (preference bypass) and E9 (quiet-hours / DST). Europe/Kyiv
observes DST, so the boundary cases here are the real ones for the
pilot, not a synthetic zone.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from notification_events import Category, Channel, EmailMode
from notification_service.domain.preferences import (
    SuppressReason,
    UserPreference,
    UserSettings,
    in_quiet_hours,
    next_quiet_hours_end,
    resolve,
)

KYIV = ZoneInfo("Europe/Kyiv")

# 22:00 → 07:00 local, the common overnight window.
NIGHT = UserSettings(
    timezone="Europe/Kyiv",
    quiet_hours_start=time(22, 0),
    quiet_hours_end=time(7, 0),
)
NO_QUIET = UserSettings(timezone="Europe/Kyiv")


def _at(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    """A local Kyiv wall-clock instant."""
    return datetime(y, m, d, hh, mm, tzinfo=KYIV)


def _resolve(
    category: Category,
    preference: UserPreference | None,
    settings: UserSettings = NO_QUIET,
    now: datetime | None = None,
    has_email_address: bool = True,
):
    return resolve(
        category=category,
        preference=preference,
        settings=settings,
        now=now or datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        has_email_address=has_email_address,
    )


# ── quiet-hours window arithmetic ───────────────────────────────────


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(21, False), (22, True), (23, True), (0, True), (6, True), (7, False), (12, False)],
)
def test_overnight_window_membership(hour: int, expected: bool) -> None:
    assert in_quiet_hours(NIGHT, _at(2026, 7, 19, hour)) is expected


def test_daytime_window_does_not_wrap() -> None:
    day = UserSettings(
        timezone="Europe/Kyiv", quiet_hours_start=time(9, 0), quiet_hours_end=time(17, 0)
    )
    assert in_quiet_hours(day, _at(2026, 7, 19, 12)) is True
    assert in_quiet_hours(day, _at(2026, 7, 19, 8)) is False
    assert in_quiet_hours(day, _at(2026, 7, 19, 20)) is False


def test_unset_window_is_never_quiet() -> None:
    assert in_quiet_hours(NO_QUIET, _at(2026, 7, 19, 3)) is False


def test_zero_width_window_silences_nothing() -> None:
    """start == end must not mean 'always quiet' — that has no visible cause."""
    degenerate = UserSettings(
        timezone="Europe/Kyiv", quiet_hours_start=time(9, 0), quiet_hours_end=time(9, 0)
    )
    assert in_quiet_hours(degenerate, _at(2026, 7, 19, 9)) is False
    assert in_quiet_hours(degenerate, _at(2026, 7, 19, 3)) is False


def test_bad_timezone_falls_back_instead_of_raising() -> None:
    """Bad tz data must not stop delivery."""
    broken = UserSettings(
        timezone="Mars/Olympus_Mons",
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(7, 0),
    )
    assert in_quiet_hours(broken, _at(2026, 7, 19, 23)) is True


# ── DST boundaries (E9) ─────────────────────────────────────────────


def test_deferral_across_spring_forward() -> None:
    """Kyiv springs forward 2026-03-29: 03:00 local never happens.

    A mail held at 23:00 the night before must still resolve to 07:00
    LOCAL the next morning — an offset-based calculation lands an hour
    out.
    """
    at = _at(2026, 3, 28, 23)
    end = next_quiet_hours_end(NIGHT, at)
    assert end.astimezone(KYIV).hour == 7
    assert end > at


def test_deferral_across_fall_back() -> None:
    """Kyiv falls back 2026-10-25: 03:00 local happens twice."""
    at = _at(2026, 10, 24, 23)
    end = next_quiet_hours_end(NIGHT, at)
    assert end.astimezone(KYIV).hour == 7
    assert end > at


def test_deferral_same_morning_when_before_end() -> None:
    """03:00 defers to 07:00 today, not tomorrow."""
    at = _at(2026, 7, 19, 3)
    end = next_quiet_hours_end(NIGHT, at).astimezone(KYIV)
    assert (end.date(), end.hour) == (at.date(), 7)


def test_deferral_next_morning_when_after_start() -> None:
    """23:00 defers to 07:00 TOMORROW."""
    at = _at(2026, 7, 19, 23)
    end = next_quiet_hours_end(NIGHT, at).astimezone(KYIV)
    assert end.day == 20
    assert end.hour == 7


# ── channel resolution (E8) ─────────────────────────────────────────


def test_defaults_apply_when_user_never_expressed_an_opinion() -> None:
    in_app, email = _resolve(Category.REPORT_SIGNED, None)
    assert in_app.dispatch is True
    assert email.dispatch is True


def test_email_off_is_honoured() -> None:
    """The core E8 case: off means off."""
    _in_app, email = _resolve(
        Category.REPORT_SIGNED,
        UserPreference(in_app_enabled=True, email_mode=EmailMode.OFF),
    )
    assert email.dispatch is False
    assert email.reason is SuppressReason.PREFERENCE


def test_in_app_off_is_honoured() -> None:
    in_app, _email = _resolve(
        Category.REPORT_AMENDED,
        UserPreference(in_app_enabled=False, email_mode=EmailMode.OFF),
    )
    assert in_app.dispatch is False
    assert in_app.reason is SuppressReason.PREFERENCE


def test_missing_email_address_suppresses_with_a_distinct_reason() -> None:
    _in_app, email = _resolve(
        Category.REPORT_SIGNED,
        UserPreference(in_app_enabled=True, email_mode=EmailMode.IMMEDIATE),
        has_email_address=False,
    )
    assert email.dispatch is False
    assert email.reason is SuppressReason.NO_EMAIL_ADDRESS


def test_quiet_hours_defers_email_but_never_in_app() -> None:
    in_app, email = _resolve(
        Category.REPORT_SIGNED,
        UserPreference(in_app_enabled=True, email_mode=EmailMode.IMMEDIATE),
        settings=NIGHT,
        now=_at(2026, 7, 19, 23),
    )
    assert in_app.dispatch is True
    assert in_app.not_before is None, "a badge is not an interruption"

    assert email.dispatch is True, "deferred, not dropped"
    assert email.reason is SuppressReason.QUIET_HOURS
    assert email.not_before is not None
    assert email.not_before.astimezone(KYIV).hour == 7


def test_digest_mode_stands_down_the_immediate_channel() -> None:
    _in_app, email = _resolve(
        Category.REPORT_AMENDED,
        UserPreference(in_app_enabled=True, email_mode=EmailMode.DIGEST),
    )
    assert email.dispatch is False
    assert email.reason is SuppressReason.DIGEST_DEFERRED


def test_digest_mode_cannot_downgrade_an_alert_into_nothing() -> None:
    """A preference may batch routine news; it may not mute a failure."""
    _in_app, email = _resolve(
        Category.REPORT_SIGNING_FAILED,
        UserPreference(in_app_enabled=True, email_mode=EmailMode.DIGEST),
    )
    assert email.dispatch is True, "a non-digestible category must still send"
    assert email.reason is None


def test_chain_failure_still_emails_admins_under_digest_preference() -> None:
    _in_app, email = _resolve(
        Category.REPORT_CHAIN_FAILURE,
        UserPreference(in_app_enabled=True, email_mode=EmailMode.DIGEST),
    )
    assert email.dispatch is True


def test_decision_channels_are_labelled_correctly() -> None:
    in_app, email = _resolve(Category.REPORT_SIGNED, None)
    assert in_app.channel is Channel.IN_APP
    assert email.channel is Channel.EMAIL
