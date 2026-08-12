"""Turn a request or a booking into the exact variables the mail shows.

Everything a template needs is computed HERE and frozen into the outbox
row, rather than re-derived by the worker from the request. A retry
three hours later — or after a deploy that changed a date format — then
sends precisely the mail the first attempt would have, which is the
property that makes "did they get the right time?" answerable from the
database instead of from a mail client.

Pure: no clock, no I/O. The caller supplies `now`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from . import copy


def _base(url: str) -> str:
    return url.rstrip("/")


def unsubscribe_url(*, public_base_url: str, token: str) -> str:
    return f"{_base(public_base_url)}/unsubscribe?t={quote(token)}"


def privacy_url(*, public_base_url: str) -> str:
    return f"{_base(public_base_url)}/legal/privacy"


def ics_url(*, public_base_url: str, token: str) -> str:
    return f"{_base(public_base_url)}/api/marketing/public/demo/{quote(token)}.ics"


def cancel_url(*, reply_to: str, lang: str) -> str:
    """A mailto, deliberately — not a self-serve cancel endpoint.

    The booking lives in Google Calendar, and we hold no credential that
    can delete an event there. A "Cancel" button that only marked a row
    in OUR database would leave the slot blocked and the host expecting
    someone: a cancellation that does not reach the calendar is worse
    than no cancel button at all. Google's own confirmation carries the
    real one; this reaches a human who can act on it either way.
    """
    subjects = {
        "en": "Cancel my Klarnote demo",
        "de": "Klarnote-Demo absagen",
        "uk": "Скасувати демо Klarnote",
    }
    subject = subjects.get(lang, subjects["en"])
    return f"mailto:{reply_to}?{urlencode({'subject': subject})}"


def add_to_calendar_url(
    *, start: datetime, end: datetime, meet_url: str, summary: str = "Klarnote — product demo"
) -> str:
    """Google's "add to calendar" prefill.

    Times are converted to UTC and stamped with a trailing Z. The
    endpoint accepts local times too, but only alongside a `ctz`
    parameter — and a prefill that silently drops the timezone puts the
    demo in the reader's own zone at the host's wall-clock hour, which
    is the one mistake here that nobody notices until the call.
    """
    fmt = "%Y%m%dT%H%M%SZ"
    params = {
        "action": "TEMPLATE",
        "text": summary,
        "dates": f"{start.astimezone(UTC):{fmt}}/{end.astimezone(UTC):{fmt}}",
        "details": f"Google Meet: {meet_url}" if meet_url else "",
        "location": meet_url,
    }
    query = urlencode({k: v for k, v in params.items() if v})
    return f"https://calendar.google.com/calendar/render?{query}"


def request_received_fields(
    *,
    lang: str,
    email: str,
    first_name: str | None,
    token: str,
    requested_at: datetime,
    booking_url: str,
    public_base_url: str,
    host_name: str,
    demo_minutes: int,
) -> dict[str, str]:
    return {
        "greeting": copy.greeting(first_name, lang),
        "email": email,
        "requested_at": copy.short_datetime(requested_at, lang),
        "booking_url": booking_url,
        "host_name": copy.host_name_for(host_name, lang),
        # The nominal slot length, not a measured one — nothing is booked
        # yet. Only the plain-text alternate reads it; the HTML says it in
        # translated prose.
        "duration_label": copy.duration_label(demo_minutes, lang),
        "unsubscribe_url": unsubscribe_url(public_base_url=public_base_url, token=token),
        "privacy_url": privacy_url(public_base_url=public_base_url),
        # The layout builds its @font-face URLs from this (templates/_layout.html):
        # the webfonts are served from the public origin, so the letter cannot
        # hard-code a host it might not be deployed under.
        "public_base_url": public_base_url,
    }


def contact_internal_fields(
    *,
    email: str,
    first_name: str | None,
    organisation: str | None,
    message: str,
    reason: str | None,
    form_lang: str,
    requested_at: datetime,
    source_page: str | None,
    public_base_url: str,
) -> dict[str, str]:
    """The sales mailbox's copy of a contact-form enquiry.

    Three things make this letter different from every other one here.

    ALWAYS ENGLISH. `form_lang` is data in the body, not the language the
    letter is written in: the sales mailbox is read by one team, and a
    notification that switches language with the sender is a notification
    nobody can skim. The visitor's own words are of course untouched.

    NO UNSUBSCRIBE. It is internal transactional mail to our own mailbox;
    an unsubscribe link on it would be an unsubscribe link on our own inbox.
    `unsubscribe_url` is therefore absent, which is also why the template
    must not reference it — StrictUndefined would fail the render.

    REPLY-TO IS THE VISITOR. `reply_to` is read by the delivery worker, not
    by the template. It is what makes answering the enquiry a reply rather
    than a copy-paste of an address out of a table.
    """
    return {
        "email": email,
        "reply_to": email,
        # An empty string rather than a placeholder: the template renders the
        # row only when the value is truthy, so a missing name shows no row at
        # all instead of a row reading "—".
        "first_name": (first_name or "").strip(),
        "organisation": (organisation or "").strip(),
        "reason": (reason or "").strip(),
        "message": message,
        # The language tag the form was in — the one useful hint about which
        # language to reply in, since the message itself may be short enough
        # to be ambiguous.
        "form_lang": form_lang,
        "requested_at": copy.short_datetime(requested_at, "en"),
        "source_page": (source_page or "").strip(),
        "public_base_url": public_base_url,
    }


def demo_confirmed_fields(
    *,
    lang: str,
    first_name: str | None,
    token: str,
    starts_at: datetime,
    ends_at: datetime,
    timezone_name: str,
    meet_url: str,
    booking_url: str,
    public_base_url: str,
    host_name: str,
    reply_to: str,
    demo_minutes: int,
) -> dict[str, str]:
    # The stored times are absolute; render them in the event's own zone
    # so "15:00 Kyiv" is what the host and the prospect both see. Falling
    # back to whatever tzinfo the datetime carries is deliberate — a
    # booking whose TZID we could not parse is still better shown in UTC
    # than not shown.
    local_start = starts_at
    local_end = ends_at
    if timezone_name:
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(timezone_name)
            local_start = starts_at.astimezone(zone)
            local_end = ends_at.astimezone(zone)
        except Exception:  # noqa: BLE001 — unknown zone, keep the stored one
            pass

    minutes = max(1, int((ends_at - starts_at).total_seconds() // 60)) or demo_minutes
    return {
        "greeting": copy.greeting(first_name, lang),
        "date_label": copy.long_date(local_start, lang),
        "time_label": copy.time_range(local_start, local_end, timezone_name),
        "duration_label": copy.duration_label(minutes, lang),
        "meet_url": meet_url,
        "add_to_calendar_url": add_to_calendar_url(
            start=starts_at, end=ends_at, meet_url=meet_url
        ),
        "ics_url": ics_url(public_base_url=public_base_url, token=token),
        "cancel_url": cancel_url(reply_to=reply_to, lang=lang),
        "booking_url": booking_url,
        "host_name": copy.host_name_for(host_name, lang),
        "unsubscribe_url": unsubscribe_url(public_base_url=public_base_url, token=token),
        "privacy_url": privacy_url(public_base_url=public_base_url),
        "public_base_url": public_base_url,
    }


# ── The .ics we attach ───────────────────────────────────────────────
def _ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 caps a content line at 75 octets.

    Folding on CHARACTERS would split a Cyrillic summary mid-codepoint
    once encoded, so this measures the UTF-8 length and breaks on a
    character boundary that keeps each chunk under the limit.
    """
    if len(line.encode("utf-8")) <= 75:
        return line
    chunks: list[str] = []
    current = ""
    limit = 74  # leaves room for the leading space on continuations
    for char in line:
        if len((current + char).encode("utf-8")) > limit:
            chunks.append(current)
            current = char
        else:
            current += char
    if current:
        chunks.append(current)
    return "\r\n ".join(chunks)


def build_ics(
    *,
    uid: str,
    starts_at: datetime,
    ends_at: datetime,
    meet_url: str,
    organizer: str,
    stamp: datetime,
    summary: str = "Klarnote — product demo",
) -> str:
    """A single-event calendar the prospect can save.

    METHOD:PUBLISH, not REQUEST. REQUEST means "you are invited, please
    reply", and a client that honours it would treat this as a second,
    competing invitation to an event Google already invited them to —
    producing a duplicate in their calendar and an RSVP back to us for a
    meeting we do not organise. PUBLISH means "here is an event, save it
    if you like", which is what an attachment on a confirmation is.
    """
    fmt = "%Y%m%dT%H%M%SZ"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Klarnote//Demo booking//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{_ics_escape(uid)}",
        f"DTSTAMP:{stamp.astimezone(UTC):{fmt}}",
        f"DTSTART:{starts_at.astimezone(UTC):{fmt}}",
        f"DTEND:{ends_at.astimezone(UTC):{fmt}}",
        f"SUMMARY:{_ics_escape(summary)}",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
    ]
    if meet_url:
        lines.append(f"LOCATION:{_ics_escape(meet_url)}")
        lines.append(f"DESCRIPTION:{_ics_escape('Google Meet: ' + meet_url)}")
    if organizer:
        lines.append(f"ORGANIZER:mailto:{organizer}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    # CRLF throughout — RFC 5545 requires it, and Outlook rejects a
    # calendar that uses bare newlines.
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
