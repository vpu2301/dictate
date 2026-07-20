"""Cross-worker WebSocket fan-out over Redis pub/sub (ADR-0030).

A notification materialised on worker A must reach a user whose socket
is pinned to worker B. Rather than track which worker owns which socket
— state that is wrong the moment a worker dies — every worker subscribes
to one pattern and forwards to whatever sockets it happens to hold. A
worker with no socket for that user simply does nothing.

Pub/sub is deliberately fire-and-forget: it is an OPTIMISATION, never
the source of truth. A dropped frame costs a client a live update, and
the client recovers on its next REST poll or on reconnect, because the
unread count always comes from the database (E5).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from notification_events import user_channel

logger = logging.getLogger(__name__)

# Every worker subscribes to this ONE pattern rather than to a channel
# per connected user. Per-user subscribe/unsubscribe on every socket
# open and close would make connection churn into Redis command churn.
_PATTERN = "mdx:notify:user:*"


async def publish_new_notification(
    redis: Any, *, tenant_id: UUID, notification_id: UUID, recipient_user_id: UUID
) -> None:
    """Announce a new notification to whichever worker holds the socket.

    The frame carries IDS ONLY. The receiving worker re-reads the row
    under that user's tenant scope before sending anything, so pub/sub —
    which has no tenant isolation of its own — never carries content and
    cannot become a cross-tenant leak.
    """
    await _publish(
        redis,
        notification_id=notification_id,
        recipient_user_id=recipient_user_id,
        tenant_id=tenant_id,
        kind="notification",
    )


async def publish_unread_changed(
    redis: Any, *, tenant_id: UUID, recipient_user_id: UUID
) -> None:
    await _publish(
        redis,
        tenant_id=tenant_id,
        recipient_user_id=recipient_user_id,
        kind="unread_count",
    )


async def _publish(
    redis: Any,
    *,
    kind: str,
    tenant_id: UUID,
    recipient_user_id: UUID,
    notification_id: UUID | None = None,
) -> None:
    # The channel is ALWAYS keyed by recipient — that is what a
    # subscribing worker can match against the sockets it holds. Keying
    # by anything else (a notification id) would publish to a channel
    # nobody is listening on, and the frame would silently vanish.
    payload = {
        "kind": kind,
        "tenant_id": str(tenant_id),
        "notification_id": str(notification_id) if notification_id else None,
        "recipient_user_id": str(recipient_user_id),
    }
    try:
        await redis.publish(user_channel(recipient_user_id), json.dumps(payload))
    except Exception as exc:  # noqa: BLE001
        # Never let a fan-out failure break materialisation. The row is
        # already committed; the client will see it on next poll.
        logger.warning("fanout.publish_failed", extra={"error": str(exc)})


class FanoutBridge:
    """Subscribes once per worker and dispatches to local sockets."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis
        self._task: asyncio.Task[None] | None = None
        self._pubsub: Any | None = None
        self._handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def set_handler(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._handler = handler

    async def start(self) -> None:
        self._pubsub = self._redis.pubsub()
        await self._pubsub.psubscribe(_PATTERN)
        self._task = asyncio.create_task(self._run(), name="notification-fanout")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.punsubscribe(_PATTERN)
                await self._pubsub.aclose()
            self._pubsub = None

    async def _run(self) -> None:  # pragma: no cover — I/O loop
        assert self._pubsub is not None
        while True:
            try:
                raw = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("fanout.recv_failed", extra={"error": str(exc)})
                await asyncio.sleep(1)
                continue

            if raw is None or self._handler is None:
                continue
            try:
                data = raw.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                await self._handler(json.loads(data))
            except Exception as exc:  # noqa: BLE001
                logger.warning("fanout.dispatch_failed", extra={"error": str(exc)})
