"""Outbox drain: render, send, retry, dead-letter.

Claiming uses ``FOR UPDATE SKIP LOCKED`` so N replicas take disjoint
work, and the claim is held until the send is recorded — a crash
mid-SMTP rolls it back rather than losing the mail.

The one thing this must never do is send twice. A prospect who gets two
identical "your demo is booked" mails learns something true about our
engineering and nothing good. That requirement is what sets the
transaction boundary at ONE ROW, not one batch: with a batch-wide
transaction, a failure on the fifth message rolls back the four sends
already recorded before it — the mail is gone from our database but not
from the recipients' inboxes, and the next poll sends all four again.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from .. import metrics
from ..adapters import mail_images
from ..adapters.email import (
    EmailDeliveryError,
    EmailPermanentError,
    EmailProvider,
    OutboundEmail,
)
from ..adapters.templates import render
from ..config import settings
from ..domain import compose, copy
from ..domain import repository as repo

logger = logging.getLogger(__name__)


def unsubscribe_header(url: str, *, one_click: bool, mailto: str) -> tuple[str, bool]:
    """The List-Unsubscribe value and whether it may claim one-click.

    Three cases, in the order they are decided.

    RFC 8058 one-click is HTTPS-only, and Gmail and Yahoo enforce that.
    Anything else — an http:// origin, a localhost URL from a dev config that
    leaked into a run wired to a real relay — is not merely ignored: it is an
    invalid header on a message that also carries links to the same origin,
    which is a strong spam signal.

    An https:// URL that nothing SERVES is the worse trap, and the one this
    deployment fell into: the header is well-formed, so it is believed, and
    Gmail's POST to it fails. The mail is then accepted with a 250 by the
    submission relay and dropped or junked at the far end — every row in the
    outbox reads `sent` while nobody receives anything, which is exactly the
    failure that is invisible from inside the service. `one_click` therefore
    has to be asserted by configuration (settings.unsubscribe_one_click), not
    inferred from the URL: only a human knows whether the route is deployed.

    Otherwise fall back to a mailto:. It is a valid List-Unsubscribe value, it
    reaches a person who can act on it, and it cannot be broken by a web origin
    that is parked, uncertificated or simply not built yet. It is deliberately
    NOT paired with the one-click header: RFC 8058 does not allow mailto there.
    """
    value = (url or "").strip()
    address = (mailto or "").strip()
    fallback = f"mailto:{address}?subject=unsubscribe" if address else ""

    if not value:
        return fallback, False
    if not value.lower().startswith("https://"):
        logger.warning("demo.unsubscribe_url_not_https", extra={"url": value[:120]})
        return fallback, False
    if not one_click:
        # The URL is well-formed but unproven. Keep the letter honest rather
        # than keep the link: a mailto that works beats an https that 404s.
        return fallback, False
    return value, True


def backoff_delay(attempt_count: int, *, base_s: float) -> timedelta:
    """Exponential, capped at an hour.

    Capped because uncapped doubling reaches days by attempt 10, and a
    demo confirmation that arrives after the demo is worse than one that
    fails loudly.
    """
    return timedelta(seconds=min(base_s * (2**attempt_count), 3600.0))


async def _ics_attachment(
    conn: asyncpg.Connection, *, booking_id: Any, now: datetime
) -> tuple[tuple[str, str, str], ...]:
    """The .ics that rides along with a confirmation.

    Attached as well as linked. A link needs a working browser and a
    reachable host at the moment the reader wants it; the attachment
    works on a phone in a lift, which is where a lot of these are read.
    """
    if booking_id is None:
        return ()
    row = await conn.fetchrow(
        """
        SELECT ical_uid, starts_at, ends_at, meet_url, summary
        FROM marketing.demo_bookings WHERE id = $1
        """,
        booking_id,
    )
    if row is None:
        return ()
    body = compose.build_ics(
        uid=row["ical_uid"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        meet_url=row["meet_url"] or "",
        organizer=settings.email_reply_to,
        stamp=now,
        summary=row["summary"] or "Klarnote — product demo",
    )
    return (("klarnote-demo.ics", "calendar", body),)


async def deliver_once(
    *,
    pool: asyncpg.Pool,
    provider: EmailProvider,
    limit: int = 25,
    now: datetime | None = None,
) -> int:
    """Drain up to `limit` rows, each in its own transaction.

    One row at a time rather than one claim of `limit` rows: see the
    module docstring for why the batch-wide transaction is a
    duplicate-send bug. It also keeps the row lock — and the open
    transaction Postgres has to hold open behind it — to the length of a
    single SMTP exchange instead of the whole batch.
    """
    at = now or datetime.now(UTC)
    processed = 0

    async with pool.acquire() as conn:
        for _ in range(limit):
            async with conn.transaction():
                rows = await repo.claim_due_outbox(conn, limit=1, now=at)
                if not rows:
                    return processed
                await _deliver_one(conn, row=rows[0], provider=provider, at=at)
                processed += 1

    return processed


async def _deliver_one(
    conn: asyncpg.Connection,
    *,
    row: asyncpg.Record,
    provider: EmailProvider,
    at: datetime,
) -> None:
    outbox_id = row["id"]
    kind = str(row["kind"])
    lang = str(row["lang"])
    attempt = int(row["attempt_count"])
    fields = repo.render_fields_of(row["render_fields"])

    # The wordmark and the hero, as `cid:` parts. Resolved HERE and not frozen
    # into render_fields at enqueue time like everything else: they are
    # deployed artefacts, not facts about this request, so a letter retried
    # after a redesign should carry today's marks — and an outbox row written
    # before they existed must not need a migration to send.
    image_context, inline_images = mail_images.for_letter(kind, lang)

    try:
        rendered = render(
            kind,
            lang,
            subject=copy.subject_for(kind, lang),
            text_body=copy.text_body(kind, lang, fields),
            context={**fields, **image_context},
        )
    except Exception as exc:  # noqa: BLE001
        # A render failure is a code or data bug, not a transient one.
        # Retrying it five times only delays the moment somebody looks.
        metrics.mail_dead_lettered.add(1, {"kind": kind, "reason": "render"})
        logger.exception("demo.render_failed", extra={"kind": kind, "lang": lang})
        await repo.mark_outbox_dead(conn, outbox_id=outbox_id, error=f"render: {exc}")
        return

    attachments = await _ics_attachment(conn, booking_id=row["booking_id"], now=at)

    unsubscribe_value, unsubscribe_one_click = unsubscribe_header(
        str(fields.get("unsubscribe_url", "")),
        one_click=settings.unsubscribe_one_click,
        mailto=settings.email_reply_to,
    )

    metrics.mail_attempts.add(1, {"kind": kind, "lang": lang})
    try:
        result = await provider.send(
            OutboundEmail(
                to_address=str(row["to_address"]),
                subject=rendered.subject,
                text_body=rendered.text_body,
                html_body=rendered.html_body,
                # Per-mail Reply-To, defaulting to the sales mailbox. Only the
                # internal contact notice sets its own: that letter goes TO us
                # and is about a visitor, so replying to it must reach them and
                # not bounce around our own mailbox. Read from the frozen
                # render fields rather than the request row, so a retry three
                # hours later still addresses the same person.
                reply_to=str(fields.get("reply_to") or settings.email_reply_to),
                list_unsubscribe=unsubscribe_value,
                list_unsubscribe_one_click=unsubscribe_one_click,
                attachments=attachments,
                inline_images=tuple(
                    (image.cid, image.filename, image.data) for image in inline_images
                ),
            )
        )
    except EmailPermanentError as exc:
        metrics.mail_failures.add(1, {"kind": kind, "permanence": "permanent"})
        metrics.mail_dead_lettered.add(1, {"kind": kind, "reason": "permanent"})
        logger.error("demo.mail_permanent_failure", extra={"kind": kind, "error": str(exc)[:200]})
        await repo.mark_outbox_dead(conn, outbox_id=outbox_id, error=str(exc))
        return
    except (EmailDeliveryError, Exception) as exc:  # noqa: BLE001
        metrics.mail_failures.add(1, {"kind": kind, "permanence": "transient"})
        next_attempt = attempt + 1
        if next_attempt >= settings.delivery_max_attempts:
            metrics.mail_dead_lettered.add(1, {"kind": kind, "reason": "max_attempts"})
            logger.error(
                "demo.mail_dead_lettered",
                extra={"kind": kind, "attempts": next_attempt, "error": str(exc)[:200]},
            )
            await repo.mark_outbox_dead(conn, outbox_id=outbox_id, error=str(exc))
            return
        await repo.mark_outbox_retry(
            conn,
            outbox_id=outbox_id,
            error=str(exc),
            next_attempt_at=at
            + backoff_delay(next_attempt, base_s=settings.delivery_backoff_base_s),
        )
        logger.warning(
            "demo.mail_retry",
            extra={"kind": kind, "attempts": next_attempt, "error": str(exc)[:200]},
        )
        return

    await repo.mark_outbox_sent(
        conn,
        outbox_id=outbox_id,
        provider_message_id=result.provider_message_id,
        at=at,
    )
    logger.info("demo.mail_sent", extra={"kind": kind, "lang": lang})


async def run_forever(*, state: Any) -> None:  # pragma: no cover — I/O loop
    while True:
        try:
            await deliver_once(
                pool=state.app_pool,
                provider=state.email_provider,
                limit=settings.delivery_batch_size,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("demo.delivery_iteration_failed")
        await asyncio.sleep(settings.delivery_poll_interval_s)
