"""A small, deliberate iCalendar reader for Google invitations.

Scope is narrow on purpose. This parses the `text/calendar` part Google
attaches to an appointment-schedule invitation and answers five
questions: which event, when, who booked, where the Meet room is, and
whether it was cancelled. It does not implement RRULE, VALARM, VTODO,
VFREEBUSY or timezone definitions — a demo booking is a single
non-recurring event, and a general-purpose parser would be far more code
carrying far more ways to be wrong about the one case we have.

Everything is pure. The watcher hands in bytes and gets back a value
object, which is what makes "does a reschedule re-send?" a unit test
rather than a calendar experiment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Property names we read. Anything else is skipped without complaint —
# Google sends a couple of dozen and new ones appear without notice.
_WANTED: Final[frozenset[str]] = frozenset(
    {
        "UID",
        "SEQUENCE",
        "DTSTART",
        "DTEND",
        "DURATION",
        "SUMMARY",
        "DESCRIPTION",
        "LOCATION",
        "STATUS",
        "METHOD",
        "ATTENDEE",
        "ORGANIZER",
        "X-GOOGLE-CONFERENCE",
    }
)

_MEET_RE: Final = re.compile(r"https://meet\.google\.com/[a-z0-9\-]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CalendarInvite:
    uid: str
    sequence: int
    starts_at: datetime
    ends_at: datetime
    attendees: tuple[str, ...]
    organizer: str = ""
    summary: str = ""
    meet_url: str = ""
    timezone_name: str = ""
    cancelled: bool = False


def unfold(raw: str) -> list[str]:
    """Undo RFC 5545 line folding.

    A continuation line begins with a space or tab and belongs to the
    line before it. Google folds at 75 octets, so a Meet URL or a long
    summary is split across lines in practice, not just in theory — a
    parser that reads lines naively finds `X-GOOGLE-CONFERENC` and a
    fragment of a URL.
    """
    lines: list[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _unescape(value: str) -> str:
    out = value.replace("\\N", "\n").replace("\\n", "\n")
    return out.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    """`DTSTART;TZID=Europe/Kyiv:20260820T150000` → name, params, value."""
    head, sep, value = line.partition(":")
    if not sep:
        return None
    parts = head.split(";")
    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for param in parts[1:]:
        key, _, val = param.partition("=")
        params[key.strip().upper()] = val.strip().strip('"')
    return name, params, value


def _parse_dt(value: str, params: dict[str, str]) -> datetime | None:
    """The three forms Google emits, and nothing else.

    An unknown TZID falls back to UTC rather than raising: a mail we
    cannot fully parse should still produce a booking with roughly the
    right time, because the alternative is a prospect who booked a demo
    and heard nothing.
    """
    raw = value.strip()
    if raw.endswith("Z"):
        try:
            return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            return None

    if params.get("VALUE") == "DATE":
        try:
            naive = datetime.strptime(raw, "%Y%m%d")
        except ValueError:
            return None
    else:
        try:
            naive = datetime.strptime(raw, "%Y%m%dT%H%M%S")
        except ValueError:
            return None

    tzid = params.get("TZID", "")
    if tzid:
        try:
            return naive.replace(tzinfo=ZoneInfo(tzid))
        except (ZoneInfoNotFoundError, ValueError):
            return naive.replace(tzinfo=UTC)
    return naive.replace(tzinfo=UTC)


def _parse_duration(value: str) -> timedelta | None:
    """`PT40M`, `PT1H30M`, `P1D`. Weeks and negatives are not emitted here."""
    match = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip().upper()
    )
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _mailto(value: str) -> str:
    address = value.strip()
    if address.lower().startswith("mailto:"):
        address = address[7:]
    return address.strip().strip("<>").lower()


def parse_invite(text: str, *, default_minutes: int = 40) -> CalendarInvite | None:
    """Read the first VEVENT. Returns None when there isn't a usable one.

    Only the FIRST event is read. An appointment-schedule booking is
    always one event, and a mail carrying several is something other
    than a demo booking — better to ignore it than to guess which one
    the prospect meant.
    """
    fields: dict[str, str] = {}
    params_by_name: dict[str, dict[str, str]] = {}
    attendees: list[str] = []
    method = ""
    in_event = False

    for line in unfold(text):
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            if in_event:
                break  # second event — stop, we only read the first
            in_event = True
            continue
        if stripped == "END:VEVENT":
            break

        parsed = _split_property(stripped)
        if parsed is None:
            continue
        name, params, value = parsed
        if name not in _WANTED:
            continue

        # METHOD is a VCALENDAR property, so it appears before the event.
        if name == "METHOD" and not in_event:
            method = value.strip().upper()
            continue
        if not in_event:
            continue

        if name == "ATTENDEE":
            address = _mailto(value)
            if address:
                attendees.append(address)
            continue

        fields[name] = value
        params_by_name[name] = params

    uid = _unescape(fields.get("UID", "")).strip()
    if not uid:
        return None

    starts_at = _parse_dt(fields.get("DTSTART", ""), params_by_name.get("DTSTART", {}))
    if starts_at is None:
        return None

    ends_at = _parse_dt(fields.get("DTEND", ""), params_by_name.get("DTEND", {}))
    if ends_at is None:
        duration = _parse_duration(fields.get("DURATION", "")) or timedelta(
            minutes=default_minutes
        )
        ends_at = starts_at + duration

    try:
        sequence = int(fields.get("SEQUENCE", "0").strip() or 0)
    except ValueError:
        sequence = 0

    description = _unescape(fields.get("DESCRIPTION", ""))
    location = _unescape(fields.get("LOCATION", ""))
    meet = fields.get("X-GOOGLE-CONFERENCE", "").strip()
    if not meet:
        # Older invitations and forwarded copies carry the room only in
        # the human-readable body.
        found = _MEET_RE.search(location) or _MEET_RE.search(description)
        meet = found.group(0) if found else ""

    return CalendarInvite(
        uid=uid,
        sequence=sequence,
        starts_at=starts_at,
        ends_at=ends_at,
        attendees=tuple(dict.fromkeys(attendees)),
        organizer=_mailto(fields.get("ORGANIZER", "")),
        summary=_unescape(fields.get("SUMMARY", "")).strip(),
        meet_url=meet,
        timezone_name=params_by_name.get("DTSTART", {}).get("TZID", ""),
        cancelled=(
            method == "CANCEL"
            or fields.get("STATUS", "").strip().upper() == "CANCELLED"
        ),
    )
