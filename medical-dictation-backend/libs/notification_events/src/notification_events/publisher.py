"""Producer-side publish helper.

Lives here, beside the envelope, so all six producers share one
implementation instead of each hand-rolling an XADD with slightly
different field names.

Two deliberate constraints:

* **No redis import.** This is a leaf package; the client is duck-typed
  on ``.xadd()``. Depending on redis here would drag a broker client
  into every service that merely wants the type definitions.

* **Never raises.** A notification is strictly less important than the
  domain action that triggered it. A report finalize must not fail, or
  roll back, because the notification bus was unreachable — the whole
  reason this is a stream and not an HTTP call (ADR-0029). Failures are
  logged and swallowed; the event is lost, which is the correct
  trade-off against losing the report.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol
from uuid import UUID

from .envelope import EVENT_SCHEMA_VERSION, NotificationEvent
from .streams import NOTIFICATIONS_STREAM

logger = logging.getLogger(__name__)

# Cap the stream so a stuck consumer cannot exhaust Redis memory. Matches
# the sprint-03 asr:jobs bound.
DEFAULT_MAXLEN = 100_000


class SupportsXAdd(Protocol):
    async def xadd(self, name: str, fields: Any, **kwargs: Any) -> Any: ...


async def publish_event(
    redis: SupportsXAdd,
    event: NotificationEvent,
    *,
    stream: str = NOTIFICATIONS_STREAM,
    maxlen: int | None = DEFAULT_MAXLEN,
) -> str | None:
    """Fire one envelope onto the bus. Returns the stream id, or None on failure.

    The field layout matches what ``libs/messaging.RedisStreamsConsumer``
    expects — ``value`` plus ``h-``-prefixed headers — so the consumer can
    read it with no producer-specific special-casing.
    """
    fields: dict[bytes, bytes] = {
        b"value": event.model_dump_json().encode("utf-8"),
        b"key": str(event.resource_id).encode("utf-8"),
        b"h-tenant_id": str(event.tenant_id).encode("utf-8"),
        b"h-category": str(event.category).encode("utf-8"),
        b"h-event_id": str(event.event_id).encode("utf-8"),
        b"h-schema_version": EVENT_SCHEMA_VERSION.encode("utf-8"),
    }
    kwargs: dict[str, Any] = {}
    if maxlen is not None:
        kwargs["maxlen"] = maxlen
        kwargs["approximate"] = True

    try:
        raw = await redis.xadd(stream, fields, **kwargs)
    except Exception as exc:  # noqa: BLE001 — see the module docstring
        logger.warning(
            "notification_events.publish_failed",
            extra={
                "error": str(exc),
                "category": str(event.category),
                "tenant_id": str(event.tenant_id),
            },
        )
        return None

    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


def build_event(
    *,
    event_id: UUID,
    tenant_id: UUID,
    category: Any,
    resource_type: str,
    resource_id: UUID,
    occurred_at: Any,
    actor_user_id: UUID | None = None,
    resource_version_id: UUID | None = None,
    recipient_hints: tuple[UUID, ...] = (),
    payload: dict[str, Any] | None = None,
) -> NotificationEvent:
    """Convenience constructor keeping producer call sites to one line."""
    return NotificationEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        category=category,
        actor_user_id=actor_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_version_id=resource_version_id,
        occurred_at=occurred_at,
        recipient_hints=recipient_hints,
        payload=payload or {},
    )
