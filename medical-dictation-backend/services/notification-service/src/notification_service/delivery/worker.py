"""Outbox delivery worker: drains due rows, retries, dead-letters.

Claiming uses ``FOR UPDATE SKIP LOCKED`` inside a tenant-scoped
transaction, so N replicas take disjoint work without coordinating and
without blocking each other (E3).

Backoff is exponential on ``attempt_count``. After
``delivery_max_attempts`` the row goes `dead` and a forensic row is
written — the alert fires on that table being non-empty (E10), because
a silent dead-letter is indistinguishable from "nothing went wrong".
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from audit import Severity as AuditSeverity
from db import tenant_connection
from notification_events import Category

from .. import audit_kinds, metrics
from ..adapters.email import (
    EmailDeliveryError,
    EmailPermanentError,
    EmailProvider,
    OutboundEmail,
)
from ..adapters.templates import render_email
from ..config import settings
from ..domain import repository as repo
from ..domain.catalog import spec_for
from ..domain.dead_letters import write_dead_letter
from ..ws.fanout import publish_unread_changed

logger = logging.getLogger(__name__)


def backoff_delay(attempt_count: int, *, base_s: float) -> timedelta:
    """Exponential, capped at an hour.

    Capped because an uncapped doubling reaches days by attempt 10, and
    a notification that arrives days late is worse than one that fails
    loudly.
    """
    seconds = min(base_s * (2**attempt_count), 3600.0)
    return timedelta(seconds=seconds)


async def deliver_once(
    *,
    app_pool: asyncpg.Pool,
    tenant_id: UUID,
    provider: EmailProvider,
    audit_writer: Any,
    redis: Any = None,
    limit: int = 50,
    now: datetime | None = None,
) -> int:
    """Drain one batch for one tenant. Returns rows processed."""
    at = now or datetime.now(UTC)
    processed = 0

    async with tenant_connection(app_pool, tenant_id) as conn:
        rows = await repo.claim_due_outbox(conn, limit=limit)

        for row in rows:
            processed += 1
            channel = row["channel"]

            if channel == "in_app":
                metrics.delivery_attempts.add(1, {"channel": "in_app"})
                # In-app has nothing to dispatch: the notification row IS
                # the delivery. Mark it sent and nudge any live socket.
                await repo.mark_outbox_sent(
                    conn, outbox_id=row["id"], provider_message_id=""
                )
                continue

            await _deliver_email(
                conn,
                row=row,
                tenant_id=tenant_id,
                provider=provider,
                app_pool=app_pool,
                audit_writer=audit_writer,
                at=at,
            )

    # Fan-out AFTER the transaction commits — pushing a badge for a row
    # that then rolls back would show a count the DB disagrees with.
    if redis is not None:
        for row in rows:
            if row["channel"] == "in_app":
                await publish_unread_changed(
                    redis, tenant_id=tenant_id, recipient_user_id=row["recipient_user_id"]
                )

    return processed


async def _deliver_email(
    conn: asyncpg.Connection,
    *,
    row: asyncpg.Record,
    tenant_id: UUID,
    provider: EmailProvider,
    app_pool: asyncpg.Pool,
    audit_writer: Any,
    at: datetime,
) -> None:
    outbox_id: UUID = row["id"]
    attempt = int(row["attempt_count"])
    category = Category(row["category"])
    spec = spec_for(category)

    address = await repo.user_email(conn, row["recipient_user_id"])
    if not address:
        # Nothing to retry towards. Suppress rather than burn attempts.
        await repo.mark_outbox_dead(
            conn, outbox_id=outbox_id, error="recipient has no email address"
        )
        return

    if not spec.email_template:
        await repo.mark_outbox_dead(
            conn,
            outbox_id=outbox_id,
            error=f"category {category} has no email template",
        )
        return

    # Re-render from the fields persisted at materialisation. These are
    # exactly the allow-listed projection, so the email cannot contain
    # anything the in-app notification did not already show.
    rendered = render_email(
        category,
        template_stem=spec.email_template,
        fields=_render_fields(row["render_fields"]),
        deep_link=row["deep_link"],
    )

    metrics.delivery_attempts.add(1, {"channel": "email"})
    try:
        result = await provider.send(
            OutboundEmail(
                to_address=address,
                subject=rendered.subject,
                text_body=rendered.text_body,
                html_body=rendered.html_body,
            )
        )
    except EmailPermanentError as exc:
        metrics.delivery_failures.add(1, {"channel": "email", "kind": "permanent"})
        metrics.dead_letters.add(1, {"channel": "email", "reason": "permanent"})
        await repo.mark_outbox_dead(conn, outbox_id=outbox_id, error=str(exc))
        await _dead_letter(
            app_pool, row=row, tenant_id=tenant_id, error=str(exc), attempt=attempt + 1
        )
        await _audit(
            audit_writer,
            tenant_id,
            audit_kinds.NOTIFICATION_DEAD_LETTERED,
            row,
            {"reason": "permanent", "error": str(exc)[:200]},
            AuditSeverity.ERROR,
        )
        return
    except (EmailDeliveryError, Exception) as exc:  # noqa: BLE001
        metrics.delivery_failures.add(1, {"channel": "email", "kind": "transient"})
        next_attempt = attempt + 1
        if next_attempt >= settings.delivery_max_attempts:
            metrics.dead_letters.add(1, {"channel": "email", "reason": "max_attempts"})
            await repo.mark_outbox_dead(conn, outbox_id=outbox_id, error=str(exc))
            await _dead_letter(
                app_pool,
                row=row,
                tenant_id=tenant_id,
                error=str(exc),
                attempt=next_attempt,
            )
            await _audit(
                audit_writer,
                tenant_id,
                audit_kinds.NOTIFICATION_DEAD_LETTERED,
                row,
                {"reason": "max_attempts", "attempts": next_attempt},
                AuditSeverity.ERROR,
            )
            return

        await repo.mark_outbox_retry(
            conn,
            outbox_id=outbox_id,
            error=str(exc),
            next_attempt_at=at
            + backoff_delay(next_attempt, base_s=settings.delivery_backoff_base_s),
        )
        await _audit(
            audit_writer,
            tenant_id,
            audit_kinds.NOTIFICATION_DELIVERY_FAILED,
            row,
            {"attempts": next_attempt, "error": str(exc)[:200]},
            AuditSeverity.WARN,
        )
        return

    await repo.mark_outbox_sent(
        conn, outbox_id=outbox_id, provider_message_id=result.provider_message_id
    )
    await _audit(
        audit_writer,
        tenant_id,
        audit_kinds.NOTIFICATION_DELIVERED,
        row,
        {"channel": "email", "attempts": attempt + 1},
        AuditSeverity.INFO,
    )


def _render_fields(raw: Any) -> dict[str, str]:
    """asyncpg hands JSONB back as str unless a codec is registered."""
    if isinstance(raw, str):
        return dict(json.loads(raw))
    return dict(raw or {})


async def _dead_letter(
    app_pool: asyncpg.Pool,
    *,
    row: asyncpg.Record,
    tenant_id: UUID,
    error: str,
    attempt: int,
) -> None:
    await write_dead_letter(
        app_pool,
        source="delivery",
        envelope={
            "tenant_id": str(tenant_id),
            "category": str(row["category"]),
            "title": row["title"],
            "deep_link": row["deep_link"],
        },
        error=error,
        notification_id=row["notification_id"],
        outbox_id=row["id"],
        channel=str(row["channel"]),
        attempt_count=attempt,
    )


async def _audit(
    audit_writer: Any,
    tenant_id: UUID,
    kind: str,
    row: asyncpg.Record,
    payload: dict[str, Any],
    severity: AuditSeverity,
) -> None:
    if audit_writer is None:
        return
    try:
        await audit_writer.write_event(
            tenant_id=tenant_id,
            kind=kind,
            actor_sub=None,
            actor_role="system",
            target_kind="notification",
            target_id=str(row["notification_id"]),
            payload=payload,
            severity=severity,
        )
    except Exception as exc:  # noqa: BLE001
        # Audit is best-effort here, as it is on the authz-denied path:
        # failing to record a send must not also prevent the send.
        logger.warning("delivery.audit_failed", extra={"error": str(exc)})


async def run_forever(*, state: Any) -> None:  # pragma: no cover — I/O loop
    """Poll every tenant that has due rows."""
    while True:
        try:
            async with state.app_pool.acquire() as conn:
                # SECURITY DEFINER (migration 0051). A direct SELECT here
                # runs unscoped, so the RLS predicate casts '' to uuid and
                # raises — this loop crashed every cycle before the fn.
                tenants = await conn.fetch(
                    "SELECT tenant_id FROM notification_tenants_with_due_outbox()"
                )
            for t in tenants:
                await deliver_once(
                    app_pool=state.app_pool,
                    tenant_id=t["tenant_id"],
                    provider=state.email_provider,
                    audit_writer=state.audit_writer,
                    redis=state.redis,
                    limit=settings.delivery_batch_size,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("delivery.iteration_failed")

        await asyncio.sleep(settings.delivery_poll_interval_s)
