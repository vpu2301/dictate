"""Turn inbound calendar invitations into confirmation emails.

One cycle:

  read the mailbox → parse each text/calendar part → decide whether it is
  a demo booking at all → match the attendee to an open request → record
  the booking (the database rejects a repeat) → queue the confirmation.

Two guards decide whether a given invitation gets a Klarnote
confirmation, and both default to NOT sending. Every meeting on the
sales calendar — a supplier call, an internal review — also arrives as
an invitation, and mailing those attendees "your Klarnote demo is
booked" is a far worse failure than missing a self-booked demo that a
human can follow up by hand.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import asyncpg

from .. import metrics
from ..adapters.imap_source import CalendarMessage, fetch_calendar_messages_async
from ..config import settings
from ..domain import compose
from ..domain import repository as repo
from ..domain.ics import CalendarInvite, parse_invite

logger = logging.getLogger(__name__)


def is_demo_invite(invite: CalendarInvite, *, summary_match: str) -> bool:
    """Guard one: does this event look like a demo at all?

    Substring, case-insensitive, on the event summary. Crude, and
    deliberately so — an appointment schedule names every booking after
    the schedule itself, so the summary is stable in a way nothing else
    about an inbound invitation is.
    """
    if not summary_match:
        return True
    return summary_match.lower() in (invite.summary or "").lower()


def booker_address(invite: CalendarInvite, *, organizer_addresses: set[str]) -> str:
    """The attendee who is not us.

    An appointment booking has exactly two attendees: the host mailbox
    and the person who booked. Anything with more than one non-host
    attendee is a meeting somebody put on the sales calendar by hand,
    not a booking, and gets no confirmation.
    """
    outsiders = [a for a in invite.attendees if a and a not in organizer_addresses]
    return outsiders[0] if len(outsiders) == 1 else ""


async def handle_invite(
    conn: asyncpg.Connection,
    invite: CalendarInvite,
    *,
    organizer_addresses: set[str],
    now: datetime,
) -> bool:
    """Process one parsed invitation. True when a confirmation was queued."""
    if not is_demo_invite(invite, summary_match=settings.booking_summary_match):
        return False

    attendee = booker_address(invite, organizer_addresses=organizer_addresses)
    if not attendee:
        return False

    request = await repo.newest_open_request_for(conn, email=attendee)
    if request is None and not settings.confirm_unmatched_bookings:
        metrics.bookings_unmatched.add(1)
        logger.info(
            "demo.booking_unmatched",
            extra={"uid": invite.uid[:60], "starts_at": invite.starts_at.isoformat()},
        )
        return False

    booking = await repo.record_booking(
        conn,
        request_id=request["id"] if request else None,
        ical_uid=invite.uid,
        sequence=invite.sequence,
        attendee_email=attendee,
        starts_at=invite.starts_at,
        ends_at=invite.ends_at,
        event_timezone=invite.timezone_name,
        meet_url=invite.meet_url,
        summary=invite.summary,
        cancelled=invite.cancelled,
    )
    if booking is None:
        # Seen before. The poll window overlaps by design, so this is
        # the common case, not an error.
        return False

    metrics.bookings_detected.add(1)

    if invite.cancelled:
        # A cancellation is recorded so the .ics endpoint stops serving a
        # dead slot, but it does not trigger mail: Google already told
        # the prospect, and a second "your demo is cancelled" from us
        # would be the only thing that made it feel like a mistake.
        logger.info("demo.booking_cancelled", extra={"uid": invite.uid[:60]})
        return False

    if request is not None:
        await repo.mark_request_booked(conn, request_id=request["id"], at=now)

    # An unmatched booking (only reachable with confirm_unmatched on) has
    # no token and no chosen language. English, and a token that points
    # the unsubscribe link somewhere harmless.
    lang = str(request["lang"]) if request else "en"
    token = str(request["token"]) if request else ""
    first_name = request["first_name"] if request else None

    fields = compose.demo_confirmed_fields(
        lang=lang,
        first_name=first_name,
        token=token,
        starts_at=booking["starts_at"],
        ends_at=booking["ends_at"],
        timezone_name=booking["event_timezone"] or "",
        meet_url=booking["meet_url"] or "",
        booking_url=settings.booking_url,
        public_base_url=settings.public_base_url,
        host_name=settings.host_name,
        reply_to=settings.email_reply_to,
        demo_minutes=settings.demo_minutes,
    )
    queued = await repo.enqueue_mail(
        conn,
        kind="demo_confirmed",
        to_address=attendee,
        lang=lang,
        render_fields=fields,
        request_id=request["id"] if request else None,
        booking_id=booking["id"],
    )
    if queued is None:
        return False

    logger.info(
        "demo.booking_confirmed",
        extra={"lang": lang, "starts_at": booking["starts_at"].isoformat()},
    )
    return True


async def poll_once(*, pool: asyncpg.Pool, now: datetime | None = None) -> int:
    """One full cycle. Returns the number of confirmations queued."""
    at = now or datetime.now(UTC)
    since = at - timedelta(days=settings.imap_lookback_days)

    messages: list[CalendarMessage] = await fetch_calendar_messages_async(
        host=settings.imap_host,
        port=settings.imap_port,
        username=settings.imap_username,
        password=settings.imap_password,
        folder=settings.imap_folder,
        since=since,
    )

    organizer_addresses = {
        a.lower()
        for a in (settings.imap_username, settings.email_from, settings.email_reply_to)
        if a
    }

    queued = 0
    async with pool.acquire() as conn:
        for message in messages:
            invite = parse_invite(
                message.calendar_text, default_minutes=settings.demo_minutes
            )
            if invite is None:
                continue
            # One transaction per invitation. A malformed sixth booking
            # must not roll back the five good ones ahead of it.
            try:
                async with conn.transaction():
                    if await handle_invite(
                        conn, invite, organizer_addresses=organizer_addresses, now=at
                    ):
                        queued += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "demo.booking_handle_failed", extra={"uid": invite.uid[:60]}
                )
    return queued


async def run_forever(*, state: object) -> None:  # pragma: no cover — I/O loop
    pool = state.app_pool  # type: ignore[attr-defined]
    while True:
        try:
            queued = await poll_once(pool=pool)
            if queued:
                logger.info("demo.booking_poll", extra={"queued": queued})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # An IMAP outage must not kill the loop — bookings keep
            # arriving and the lookback window will pick them up once the
            # mailbox is reachable again.
            metrics.booking_watch_failures.add(1)
            logger.exception("demo.booking_poll_failed")
        await asyncio.sleep(settings.imap_poll_interval_s)
