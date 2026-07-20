"""Preference + quiet-hours resolution.

Deliberately PURE: every input is a parameter, including the clock. The
whole reason a "you turned email off and still got one" bug (E8) is hard
to fix is usually that the decision is spread across three call sites
with slightly different conditions. Here there is exactly one function
that decides, it takes no ambient state, and it is exhaustively unit
tested against a frozen clock — including across a DST boundary (E9).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from notification_events import Category, Channel, EmailMode

from .catalog import spec_for

DEFAULT_TIMEZONE = "Europe/Kyiv"


class SuppressReason(StrEnum):
    """Why a channel will not be dispatched. Persisted on the outbox row."""

    PREFERENCE = "preference"
    QUIET_HOURS = "quiet_hours"
    NO_EMAIL_ADDRESS = "no_email_address"
    DIGEST_DEFERRED = "digest_deferred"
    NOT_DIGEST_ELIGIBLE = "not_digest_eligible"


@dataclass(frozen=True, slots=True)
class UserPreference:
    """A user's explicit override for one category. Absent ⇒ catalog default."""

    in_app_enabled: bool
    email_mode: EmailMode


@dataclass(frozen=True, slots=True)
class UserSettings:
    """Per-user, not per-category."""

    timezone: str = DEFAULT_TIMEZONE
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    digest_hour: int = 8


@dataclass(frozen=True, slots=True)
class ChannelDecision:
    """What to write to the outbox for one channel."""

    channel: Channel
    # False ⇒ write the row as `suppressed` with `reason`, never dispatch.
    # Suppressed rows are written rather than skipped so that "we did not
    # email you, and here is why" is a queryable fact (E8).
    dispatch: bool
    reason: SuppressReason | None = None
    # When set, the row is `pending` but not due until this instant —
    # quiet-hours deferral and digest batching both use it.
    not_before: datetime | None = None


def resolve_timezone(name: str) -> ZoneInfo:
    """Never raise on bad tz data — fall back and keep delivering."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def in_quiet_hours(settings: UserSettings, at: datetime) -> bool:
    """Is `at` inside the user's local quiet window?

    Evaluated in LOCAL wall-clock time, which is what the user set. A
    window is inclusive of its start and exclusive of its end, and may
    wrap midnight (22:00 → 07:00 is the common case).
    """
    if settings.quiet_hours_start is None or settings.quiet_hours_end is None:
        return False

    local = at.astimezone(resolve_timezone(settings.timezone))
    now_t = local.time()
    start = settings.quiet_hours_start
    end = settings.quiet_hours_end

    if start == end:
        # Degenerate: a zero-width window silences nothing. Treating it as
        # "always quiet" would mean a user who set both fields equal never
        # receives another email, with no visible cause.
        return False
    if start < end:
        return start <= now_t < end
    # Wraps midnight.
    return now_t >= start or now_t < end


def next_quiet_hours_end(settings: UserSettings, at: datetime) -> datetime:
    """The first instant after `at` at which the quiet window is over.

    Computed by walking forward in LOCAL time and re-localising, so a
    window that spans a DST transition lands on the correct absolute
    instant instead of drifting by an hour (E9).
    """
    if settings.quiet_hours_end is None:
        return at

    tz = resolve_timezone(settings.timezone)
    local = at.astimezone(tz)
    candidate = datetime.combine(local.date(), settings.quiet_hours_end, tzinfo=tz)
    if candidate <= local:
        candidate = datetime.combine(
            local.date() + timedelta(days=1), settings.quiet_hours_end, tzinfo=tz
        )
    return candidate.astimezone(UTC)


def resolve(
    *,
    category: Category,
    preference: UserPreference | None,
    settings: UserSettings,
    now: datetime,
    has_email_address: bool,
) -> tuple[ChannelDecision, ChannelDecision]:
    """Decide both channels for one (user, category). Returns (in_app, email).

    `preference` is None when the user has never overridden this
    category; the catalog default applies. That is resolved here rather
    than by a DB column default so a tenant-level default change takes
    effect for everyone who never expressed an opinion.
    """
    spec = spec_for(category)

    in_app_enabled = preference.in_app_enabled if preference else spec.default_in_app
    email_mode = preference.email_mode if preference else spec.default_email_mode

    in_app = ChannelDecision(
        channel=Channel.IN_APP,
        dispatch=in_app_enabled,
        reason=None if in_app_enabled else SuppressReason.PREFERENCE,
    )
    # Quiet hours never touch in-app: a badge that increments silently is
    # not an interruption, and holding it back would make the feed lie
    # about what has happened.

    email = _resolve_email(
        category=category,
        email_mode=email_mode,
        settings=settings,
        now=now,
        has_email_address=has_email_address,
    )
    return in_app, email


def _resolve_email(
    *,
    category: Category,
    email_mode: EmailMode,
    settings: UserSettings,
    now: datetime,
    has_email_address: bool,
) -> ChannelDecision:
    spec = spec_for(category)

    def suppressed(reason: SuppressReason) -> ChannelDecision:
        return ChannelDecision(channel=Channel.EMAIL, dispatch=False, reason=reason)

    if email_mode is EmailMode.OFF:
        return suppressed(SuppressReason.PREFERENCE)

    if not has_email_address:
        return suppressed(SuppressReason.NO_EMAIL_ADDRESS)

    if email_mode is EmailMode.DIGEST:
        if not spec.digest_eligible:
            # The user asked to batch this, but the category refuses to be
            # batched (a signing failure, a chain-integrity alert). Send it
            # immediately rather than silently dropping it — a preference
            # may not downgrade an alert into nothing.
            return _immediate_or_deferred(settings, now)
        # The digest job will pick this up; the outbox row records that
        # the immediate channel deliberately stood down.
        return suppressed(SuppressReason.DIGEST_DEFERRED)

    return _immediate_or_deferred(settings, now)


def _immediate_or_deferred(settings: UserSettings, now: datetime) -> ChannelDecision:
    if in_quiet_hours(settings, now):
        # DEFERRED, not suppressed: the mail still goes, just after the
        # window closes. Dropping it would lose the notification entirely.
        return ChannelDecision(
            channel=Channel.EMAIL,
            dispatch=True,
            reason=SuppressReason.QUIET_HOURS,
            not_before=next_quiet_hours_end(settings, now),
        )
    return ChannelDecision(channel=Channel.EMAIL, dispatch=True)
