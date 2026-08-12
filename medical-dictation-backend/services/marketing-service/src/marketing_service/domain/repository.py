"""Every SQL statement this service runs.

Connections come straight from the pool, NOT through
``db.tenant_connection``. That helper exists to set ``app.tenant_id`` for
the RLS predicates in the ``public`` schema; the ``marketing`` schema has
no tenant column and its policies are role-scoped (migration 0075), so
scoping here would set a variable nothing reads and imply a tenancy that
does not exist.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg


# ── Requests ─────────────────────────────────────────────────────────
async def create_demo_request(
    conn: asyncpg.Connection,
    *,
    token: str,
    email: str,
    lang: str,
    country: str | None,
    first_name: str | None,
    organisation: str | None,
    source_page: str | None,
    ip_hash: str | None,
    user_agent: str | None,
    message: str | None = None,
    reason: str | None = None,
) -> asyncpg.Record:
    return await conn.fetchrow(
        """
        INSERT INTO marketing.demo_requests
            (token, email, lang, country, first_name, organisation,
             source_page, ip_hash, user_agent, message, reason)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id, token, email, lang, requested_at
        """,
        token,
        email,
        lang,
        country,
        first_name,
        organisation,
        source_page,
        ip_hash,
        user_agent,
        # Only the contact form sends these; demo and subscribe submissions
        # store NULL and the sales notice is never enqueued for them.
        message,
        reason,
    )


async def requests_from_ip_since(
    conn: asyncpg.Connection, *, ip_hash: str, since: datetime
) -> int:
    count: int | None = await conn.fetchval(
        """
        SELECT count(*) FROM marketing.demo_requests
        WHERE ip_hash = $1 AND requested_at >= $2
        """,
        ip_hash,
        since,
    )
    return int(count or 0)


async def newest_open_request_for(
    conn: asyncpg.Connection, *, email: str
) -> asyncpg.Record | None:
    """The request a booking most likely belongs to.

    Newest first, and `status <> 'cancelled'` rather than `= 'requested'`:
    someone who books, reschedules, then books again is still the same
    lead, and their second confirmation must go out in the language the
    first one used.
    """
    return await conn.fetchrow(
        """
        SELECT id, token, email, lang, first_name, organisation
        FROM marketing.demo_requests
        WHERE lower(email) = lower($1) AND status <> 'cancelled'
        ORDER BY requested_at DESC
        LIMIT 1
        """,
        email,
    )


async def mark_request_booked(
    conn: asyncpg.Connection, *, request_id: UUID, at: datetime
) -> None:
    await conn.execute(
        """
        UPDATE marketing.demo_requests
        SET status = 'booked', booked_at = $2
        WHERE id = $1
        """,
        request_id,
        at,
    )


# ── Bookings ─────────────────────────────────────────────────────────
async def record_booking(
    conn: asyncpg.Connection,
    *,
    request_id: UUID | None,
    ical_uid: str,
    sequence: int,
    attendee_email: str,
    starts_at: datetime,
    ends_at: datetime,
    event_timezone: str,
    meet_url: str,
    summary: str,
    cancelled: bool,
) -> asyncpg.Record | None:
    """Insert, or return None when we have seen this (uid, sequence).

    ON CONFLICT DO NOTHING is the whole idempotency story: the watcher
    re-reads messages whenever polls overlap, and a returned None means
    "already handled, send nothing" without a read-then-write race
    between two replicas.
    """
    return await conn.fetchrow(
        """
        INSERT INTO marketing.demo_bookings
            (request_id, ical_uid, sequence, attendee_email, starts_at, ends_at,
             event_timezone, meet_url, summary, cancelled)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (ical_uid, sequence) DO NOTHING
        RETURNING id, request_id, starts_at, ends_at, event_timezone, meet_url
        """,
        request_id,
        ical_uid,
        sequence,
        attendee_email,
        starts_at,
        ends_at,
        event_timezone,
        meet_url,
        summary,
        cancelled,
    )


# ── Outbox ───────────────────────────────────────────────────────────
async def enqueue_mail(
    conn: asyncpg.Connection,
    *,
    kind: str,
    to_address: str,
    lang: str,
    render_fields: dict[str, Any],
    request_id: UUID | None = None,
    booking_id: UUID | None = None,
) -> UUID | None:
    """Queue one mail. None means the partial unique index suppressed it.

    The suppression is a feature: both callers can enqueue freely and
    the database decides whether this prospect has already been told.
    """
    return await conn.fetchval(  # type: ignore[no-any-return]
        """
        INSERT INTO marketing.demo_mail_outbox
            (request_id, booking_id, kind, to_address, lang, render_fields)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        request_id,
        booking_id,
        kind,
        to_address,
        lang,
        json.dumps(render_fields, ensure_ascii=False, sort_keys=True),
    )


async def claim_due_outbox(
    conn: asyncpg.Connection, *, limit: int, now: datetime
) -> list[asyncpg.Record]:
    """FOR UPDATE SKIP LOCKED — N replicas take disjoint work.

    Must run inside a transaction the caller holds open for the whole
    send, so a crash mid-SMTP rolls the claim back rather than losing
    the mail.
    """
    rows = await conn.fetch(
        """
        SELECT id, request_id, booking_id, kind, to_address, lang,
               render_fields, attempt_count
        FROM marketing.demo_mail_outbox
        WHERE status = 'pending' AND next_attempt_at <= $2
        ORDER BY next_attempt_at
        LIMIT $1
        FOR UPDATE SKIP LOCKED
        """,
        limit,
        now,
    )
    return list(rows)


async def mark_outbox_sent(
    conn: asyncpg.Connection, *, outbox_id: UUID, provider_message_id: str, at: datetime
) -> None:
    await conn.execute(
        """
        UPDATE marketing.demo_mail_outbox
        SET status = 'sent',
            sent_at = $3,
            attempt_count = attempt_count + 1,
            provider_message_id = $2,
            last_error = NULL
        WHERE id = $1
        """,
        outbox_id,
        provider_message_id,
        at,
    )


async def mark_outbox_retry(
    conn: asyncpg.Connection, *, outbox_id: UUID, error: str, next_attempt_at: datetime
) -> None:
    await conn.execute(
        """
        UPDATE marketing.demo_mail_outbox
        SET attempt_count = attempt_count + 1,
            next_attempt_at = $3,
            last_error = $2
        WHERE id = $1
        """,
        outbox_id,
        error[:500],
        next_attempt_at,
    )


async def mark_outbox_dead(
    conn: asyncpg.Connection, *, outbox_id: UUID, error: str
) -> None:
    await conn.execute(
        """
        UPDATE marketing.demo_mail_outbox
        SET status = 'dead',
            attempt_count = attempt_count + 1,
            last_error = $2
        WHERE id = $1
        """,
        outbox_id,
        error[:500],
    )


async def dead_letter_count(conn: asyncpg.Connection) -> int:
    """Read by the readiness/metrics surface — a non-zero value is an alert.

    A dead-lettered demo mail is a prospect who asked to see the product
    and heard nothing back, which is exactly the failure that looks like
    success in every other metric.
    """
    count: int | None = await conn.fetchval(
        "SELECT count(*) FROM marketing.demo_mail_outbox WHERE status = 'dead'"
    )
    return int(count or 0)


def render_fields_of(raw: Any) -> dict[str, Any]:
    """asyncpg hands JSONB back as str unless a codec is registered."""
    if isinstance(raw, str):
        return dict(json.loads(raw))
    return dict(raw or {})
