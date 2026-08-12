"""Read calendar invitations out of the sales mailbox.

Google's appointment pages have no webhook and no callback. What they do
have is the invitation Google mails to the organiser the moment someone
books — and that message carries a `text/calendar` part with the UID,
the slot and the Meet room in it. So the mailbox is the event feed, and
the one credential that sends our mail also reads the bookings.

`imaplib` is synchronous, and that is fine: the poll runs every couple
of minutes on a thread, so the event loop is never blocked. An async
IMAP client would add a dependency to save nothing measurable.

The connection is opened READ-ONLY. A human reads this mailbox, and a
watcher that marked invitations as seen would quietly hide bookings from
the person whose job is to prepare for them.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import imaplib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import Message

logger = logging.getLogger(__name__)

# Ceiling on one poll. A cold start against a busy mailbox should not
# pull thousands of messages in a single pass; whatever is missed is
# picked up on the next cycle, still inside the lookback window.
MAX_MESSAGES_PER_POLL = 200


@dataclass(frozen=True, slots=True)
class CalendarMessage:
    """One `text/calendar` part, with just enough context to log about it."""

    message_id: str
    subject: str
    calendar_text: str


def _decode(part: Message) -> str:
    # `get_payload(decode=True)` is typed as returning the union of every
    # payload shape; on a leaf part it is bytes or None, and a multipart
    # (which cannot reach here) would return a list.
    raw = part.get_payload(decode=True)
    if not isinstance(raw, bytes | bytearray):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        # An invitation naming a charset Python does not know is still
        # mostly ASCII property names; refusing to read it would lose a
        # real booking over an encoding label.
        return raw.decode("utf-8", errors="replace")


def _calendar_parts(message: Message) -> list[str]:
    out: list[str] = []
    for part in message.walk():
        content_type = (part.get_content_type() or "").lower()
        filename = (part.get_filename() or "").lower()
        if content_type == "text/calendar" or filename.endswith(".ics"):
            text = _decode(part)
            if "BEGIN:VEVENT" in text:
                out.append(text)
    return out


def fetch_calendar_messages(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str,
    since: datetime,
    limit: int = MAX_MESSAGES_PER_POLL,
) -> list[CalendarMessage]:
    """Blocking. Call through `fetch_calendar_messages_async`."""
    found: list[CalendarMessage] = []
    client = imaplib.IMAP4_SSL(host, port)
    try:
        client.login(username, password)
        client.select(folder, readonly=True)

        # IMAP SINCE has DATE granularity, so a same-day poll re-reads
        # the day so far. That is intentional and cheap — the (uid,
        # sequence) unique index is what makes re-reading harmless, and
        # relying on it is safer than relying on a high-water mark that
        # a restart would lose.
        criterion = (since - timedelta(days=1)).strftime("%d-%b-%Y")
        typ, data = client.search(None, "SINCE", criterion)
        if typ != "OK" or not data or not data[0]:
            return found

        ids = data[0].split()[-limit:]
        for message_id in ids:
            typ, payload = client.fetch(message_id, "(RFC822)")
            if typ != "OK" or not payload:
                continue
            raw = next(
                (item[1] for item in payload if isinstance(item, tuple) and item[1]),
                None,
            )
            if not isinstance(raw, bytes | bytearray):
                continue
            parsed = email.message_from_bytes(bytes(raw))
            for calendar_text in _calendar_parts(parsed):
                found.append(
                    CalendarMessage(
                        message_id=str(parsed.get("Message-ID", "")),
                        subject=str(parsed.get("Subject", "")),
                        calendar_text=calendar_text,
                    )
                )
    finally:
        # Teardown must not mask a real error from the body above.
        with contextlib.suppress(Exception):
            client.logout()
    return found


async def fetch_calendar_messages_async(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str,
    since: datetime,
    limit: int = MAX_MESSAGES_PER_POLL,
) -> list[CalendarMessage]:
    return await asyncio.to_thread(
        fetch_calendar_messages,
        host=host,
        port=port,
        username=username,
        password=password,
        folder=folder,
        since=since,
        limit=limit,
    )
