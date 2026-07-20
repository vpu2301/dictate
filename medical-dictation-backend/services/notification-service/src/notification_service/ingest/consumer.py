"""Redis Streams consumer: envelopes in, notification rows out.

At-least-once. The DLQ path splits by cause, because the two failures
need different responses:

  * A malformed envelope will NEVER succeed. Retrying it three times
    just delays the inevitable and keeps a poison entry circulating, so
    it is dead-lettered on first sight and acked.
  * A transient failure (database unavailable) WILL succeed later, so it
    goes back to the pending-entries list to be reclaimed and retried.

Getting that distinction backwards is how a queue wedges itself behind
one bad message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg
from pydantic import ValidationError

from audit import AuditWriter
from audit import Severity as AuditSeverity
from db import tenant_connection
from messaging import Message, RedisStreamsConsumer, RedisStreamsProducer
from notification_events import (
    NOTIFICATIONS_DLQ_STREAM,
    NOTIFICATIONS_GROUP,
    NOTIFICATIONS_STREAM,
    NotificationEvent,
)

from .. import audit_kinds, metrics
from ..config import settings
from ..domain.dead_letters import write_dead_letter
from ..domain.materialize import materialize
from ..ws.fanout import publish_new_notification

logger = logging.getLogger(__name__)


class _PermanentEventError(Exception):
    """The envelope can never be processed. Dead-letter it now."""


async def build_consumer(redis: Any) -> RedisStreamsConsumer:
    producer = RedisStreamsProducer(client=redis, default_stream=NOTIFICATIONS_STREAM)
    return RedisStreamsConsumer(
        client=redis,
        producer=producer,
        stream=NOTIFICATIONS_STREAM,
        group=NOTIFICATIONS_GROUP,
        consumer=settings.consumer_name,
        dlq_stream=NOTIFICATIONS_DLQ_STREAM,
        max_retries=settings.ingest_max_retries,
        reclaim_idle_ms=settings.ingest_reclaim_idle_ms,
    )


async def run_forever(
    *,
    app_pool: asyncpg.Pool,
    audit_writer: AuditWriter,
    redis: Any,
    stop: asyncio.Event | None = None,
) -> None:  # pragma: no cover — exercised by the integration suite
    consumer = await build_consumer(redis)
    async with consumer:
        async for message in consumer:
            if stop is not None and stop.is_set():
                return
            try:
                await handle_message(
                    message,
                    app_pool=app_pool,
                    audit_writer=audit_writer,
                    redis=redis,
                )
                await consumer.ack(message)
            except _PermanentEventError as exc:
                # Never going to work. Record it and stop retrying.
                metrics.events_rejected.add(1)
                await write_dead_letter(
                    app_pool,
                    source="ingest",
                    envelope=_raw_envelope(message),
                    error=str(exc),
                )
                await consumer.ack(message)
            except Exception as exc:  # noqa: BLE001
                # Transient. Leave it pending for reclaim + retry; the
                # consumer promotes it to the DLQ once it hits the cap.
                logger.exception("ingest.transient_failure", exc_info=exc)
                await consumer.fail(message, error_kind="unhandled")


async def handle_message(
    message: Message,
    *,
    app_pool: asyncpg.Pool,
    audit_writer: AuditWriter,
    redis: Any,
) -> None:
    event = parse_event(message)

    async with tenant_connection(app_pool, event.tenant_id) as conn:
        result = await materialize(
            event,
            conn=conn,
            redis=redis,
            app_base_url=settings.app_base_url,
            rate_cap=settings.rate_cap_per_category,
            rate_window_s=settings.rate_cap_window_s,
            now=datetime.now(UTC),
        )

    # Fan out only what was actually created. Publishing on the
    # redelivery path is what would make a badge tick twice for one fact.
    metrics.events_consumed.add(1, {"category": str(event.category)})
    if result.created:
        metrics.notifications_created.add(
            len(result.created), {"category": str(event.category)}
        )
    if result.coalesced:
        metrics.coalesced.add(result.coalesced, {"category": str(event.category)})
    if result.suppressed:
        metrics.suppressed.add(result.suppressed, {"category": str(event.category)})

    for created in result.created:
        await publish_new_notification(
            redis,
            tenant_id=event.tenant_id,
            notification_id=created.notification_id,
            recipient_user_id=created.recipient_user_id,
        )

    if result.created or result.coalesced:
        await audit_writer.write_event(
            tenant_id=event.tenant_id,
            kind=audit_kinds.NOTIFICATION_MATERIALIZED,
            actor_sub=event.actor_user_id,
            actor_role="system",
            target_kind="notification",
            target_id=str(event.resource_id),
            payload={
                "category": str(event.category),
                "event_id": str(event.event_id),
                "created": len(result.created),
                "coalesced": result.coalesced,
                "duplicates": result.duplicates,
            },
            severity=AuditSeverity.INFO,
        )


def parse_event(message: Message) -> NotificationEvent:
    """Decode the envelope, or declare it permanently undeliverable."""
    try:
        return NotificationEvent.model_validate_json(message.value.decode("utf-8"))
    except (ValidationError, UnicodeDecodeError, ValueError) as exc:
        raise _PermanentEventError(f"undecodable envelope: {exc}") from exc


def _raw_envelope(message: Message) -> dict[str, Any]:
    """Best-effort structured copy of a bad envelope, for forensics."""
    try:
        parsed = json.loads(message.value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_raw": message.value.decode("utf-8", errors="replace")[:4000]}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}
