"""One domain fact → zero or more per-recipient notification rows.

Everything here is idempotent. The consumer is at-least-once, so this
function WILL be called twice with the same event, and the second call
must be a no-op that reports what the first one did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import asyncpg

from notification_events import Category, NotificationEvent

from . import render
from . import repository as repo
from .catalog import RecipientRule, spec_for
from .preferences import ChannelDecision, SuppressReason, resolve

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Created:
    """A row that this call actually inserted.

    Carries the recipient as well as the id because WebSocket fan-out is
    keyed by recipient — the pub/sub channel a worker listens on is the
    user's, so an id alone cannot be delivered.
    """

    notification_id: UUID
    recipient_user_id: UUID


@dataclass(slots=True)
class MaterializeResult:
    """What one event actually did — the unit the metrics and tests read."""

    created: list[Created] = field(default_factory=list)
    # Recipients whose row already existed: the redelivery path.
    duplicates: int = 0
    coalesced: int = 0
    # Outbox rows written as `suppressed` (auditable proof, E8).
    suppressed: int = 0
    recipients_considered: int = 0


async def resolve_recipients(
    event: NotificationEvent, conn: asyncpg.Connection
) -> list[UUID]:
    """Who hears about this fact.

    Producer-supplied hints are always filtered through the tenant's own
    user table, so a hint naming a foreign user resolves to nothing
    rather than crossing a tenant boundary.
    """
    spec = spec_for(event.category)

    match spec.recipient_rule:
        case RecipientRule.TENANT_ADMINS:
            # Resolved HERE, not by the producer: report-service has no
            # business querying who the tenant's admins are.
            recipients = await repo.tenant_admin_ids(conn)
        case RecipientRule.REPORT_PARTICIPANTS | RecipientRule.EXPLICIT_HINTS:
            # The producer owns the report row and already knows its
            # author and co-authors, so it sends them as hints. That
            # keeps notification-service out of report-service's tables
            # — a cross-service read would couple their schemas.
            recipients = await repo.filter_to_tenant_members(conn, event.recipient_hints)

    if spec.exclude_actor and event.actor_user_id is not None:
        recipients = [r for r in recipients if r != event.actor_user_id]

    # Deduplicate while preserving order: a report whose author is also
    # listed as a co-author must not be notified twice.
    seen: set[UUID] = set()
    unique: list[UUID] = []
    for r in recipients:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


async def materialize(
    event: NotificationEvent,
    *,
    conn: asyncpg.Connection,
    redis: object | None = None,
    app_base_url: str,
    rate_cap: int = 0,
    rate_window_s: int = 300,
    now: datetime | None = None,
) -> MaterializeResult:
    """Turn one event into rows. Safe to call repeatedly with the same event."""
    at = now or datetime.now(UTC)
    result = MaterializeResult()
    spec = spec_for(event.category)

    recipients = await resolve_recipients(event, conn)
    result.recipients_considered = len(recipients)
    if not recipients:
        # Not an error: plenty of facts legitimately interest nobody (a
        # solo author finalizing their own report).
        logger.debug(
            "materialize.no_recipients",
            extra={"event_id": str(event.event_id), "category": str(event.category)},
        )
        return result

    title = render.render_title(event)
    body = render.render_body(event)
    link = render.deep_link(event, base_url=app_base_url)
    severity = render.severity_for(event)
    # Persisted alongside the rendering so the email channel re-renders
    # from the same allow-listed pointers instead of parsing the title.
    fields = render.safe_payload(event)

    for recipient in recipients:
        over_cap = False
        if rate_cap > 0 and redis is not None:
            over_cap = await _over_rate_cap(
                redis,
                tenant_id=event.tenant_id,
                recipient=recipient,
                category=event.category,
                cap=rate_cap,
                window_s=rate_window_s,
            )

        if over_cap:
            await _coalesce(
                conn,
                event=event,
                recipient=recipient,
                link=link,
                severity=severity,
                at=at,
            )
            result.coalesced += 1
            continue

        notification_id = await repo.insert_notification(
            conn,
            tenant_id=event.tenant_id,
            recipient_user_id=recipient,
            category=event.category,
            dedupe_key=event.dedupe_key(recipient),
            title=title,
            body_text=body,
            deep_link=link,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            severity=severity,
            render_fields=fields,
        )
        if notification_id is None:
            # Redelivery. The first call already wrote this row AND its
            # outbox rows in the same transaction, so there is nothing
            # left to do — re-deriving the outbox here is what would
            # cause a duplicate send.
            result.duplicates += 1
            continue

        result.created.append(
            Created(notification_id=notification_id, recipient_user_id=recipient)
        )
        result.suppressed += await _write_outbox(
            conn,
            event=event,
            recipient=recipient,
            notification_id=notification_id,
            at=at,
        )

    logger.info(
        "materialize.done",
        extra={
            "event_id": str(event.event_id),
            "category": str(event.category),
            # NOT "created": logging reserves that on LogRecord and
            # raises KeyError, which threw away the whole materialisation
            # AFTER the rows were written — the event then retried forever.
            "created_count": len(result.created),
            "duplicates": result.duplicates,
            "coalesced": result.coalesced,
            "rule": str(spec.recipient_rule),
        },
    )
    return result


async def _write_outbox(
    conn: asyncpg.Connection,
    *,
    event: NotificationEvent,
    recipient: UUID,
    notification_id: UUID,
    at: datetime,
) -> int:
    """Write one outbox row per channel. Returns the suppressed count."""
    preference = await repo.load_preference(
        conn, user_id=recipient, category=event.category
    )
    settings = await repo.load_settings(conn, user_id=recipient)
    email = await repo.user_email(conn, recipient)

    in_app_decision, email_decision = resolve(
        category=event.category,
        preference=preference,
        settings=settings,
        now=at,
        has_email_address=email is not None,
    )

    suppressed = 0
    for decision in (in_app_decision, email_decision):
        suppressed += await _write_one(
            conn,
            tenant_id=event.tenant_id,
            notification_id=notification_id,
            decision=decision,
            at=at,
        )
    return suppressed


async def _write_one(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    notification_id: UUID,
    decision: ChannelDecision,
    at: datetime,
) -> int:
    if not decision.dispatch:
        # Written, not skipped: a suppressed row is the auditable proof
        # that we deliberately did not deliver, and why (E8).
        await repo.insert_outbox(
            conn,
            tenant_id=tenant_id,
            notification_id=notification_id,
            channel=decision.channel,
            status="suppressed",
            suppressed_reason=str(decision.reason or ""),
            next_attempt_at=None,
        )
        return 1

    await repo.insert_outbox(
        conn,
        tenant_id=tenant_id,
        notification_id=notification_id,
        channel=decision.channel,
        status="pending",
        # A quiet-hours deferral keeps its reason so an operator can see
        # why a pending row is not due yet.
        suppressed_reason=(
            str(decision.reason) if decision.reason is SuppressReason.QUIET_HOURS else ""
        ),
        next_attempt_at=decision.not_before or at,
    )
    return 0


async def _coalesce(
    conn: asyncpg.Connection,
    *,
    event: NotificationEvent,
    recipient: UUID,
    link: str,
    severity: str,
    at: datetime,
) -> None:
    """Fold a storm into one running row per (recipient, category, window).

    The dedupe_key is the WINDOW, not the event, so every event past the
    cap lands on the same row and simply updates its count.
    """
    bucket = int(at.timestamp())
    key = f"coalesce:{event.category}:{recipient}:{bucket // 3600}"

    existing = await repo.notification_id_for_dedupe(conn, dedupe_key=key)
    if existing is None:
        await repo.insert_notification(
            conn,
            tenant_id=event.tenant_id,
            recipient_user_id=recipient,
            category=event.category,
            dedupe_key=key,
            title=render.coalesced_title(event.category, 1),
            body_text=render.coalesced_body(1),
            deep_link=link,
            resource_type=event.resource_type,
            resource_id=None,
            severity=severity,
        )
        return

    # Bump the visible count. Re-reading it from the row keeps the
    # counter correct even across a worker restart.
    count = await conn.fetchval(
        "SELECT count(*) FROM notifications "
        " WHERE recipient_user_id = $1 AND category = $2 AND created_at >= $3",
        recipient,
        str(event.category),
        at.replace(minute=0, second=0, microsecond=0),
    )
    await repo.bump_coalesced(
        conn,
        notification_id=existing,
        title=render.coalesced_title(event.category, int(count) + 1),
        body_text=render.coalesced_body(int(count) + 1),
    )


async def _over_rate_cap(
    redis: object,
    *,
    tenant_id: UUID,
    recipient: UUID,
    category: Category,
    cap: int,
    window_s: int,
) -> bool:
    """Fixed-window counter per (tenant, recipient, category).

    Redis rather than a COUNT(*): the cap is checked on every single
    event, and a count over a growing table is exactly the query that
    gets slower as the storm gets worse.
    """
    key = f"mdx:notif:cap:{tenant_id}:{recipient}:{category}"
    count = await redis.incr(key)  # type: ignore[attr-defined]
    if int(count) == 1:
        await redis.expire(key, window_s)  # type: ignore[attr-defined]
    return int(count) > cap


__all__ = ["Created", "MaterializeResult", "materialize", "resolve_recipients"]
