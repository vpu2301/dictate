"""Redis key/stream names shared by producers and the consumer.

Centralised here so a producer and the consumer can never drift onto
different streams — the failure mode is silent (events published to a
stream nobody reads), which is exactly the kind of thing a shared
constant prevents.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

# The event bus (ADR-0029). One stream, one consumer group; horizontal
# scale comes from adding consumers to the group.
NOTIFICATIONS_STREAM: Final = "mdx:notifications:events"
NOTIFICATIONS_DLQ_STREAM: Final = "mdx:notifications:events:dlq"
NOTIFICATIONS_GROUP: Final = "notification-workers"

# Cross-worker WebSocket fan-out (ADR-0030). A notification materialised
# on worker A must reach a socket pinned to worker B, so every worker
# subscribes to the per-user pub/sub channel and forwards to whichever
# sockets it holds locally.
_USER_CHANNEL_PREFIX: Final = "mdx:notify:user:"


def user_channel(user_id: UUID | str) -> str:
    """Pub/sub channel carrying fan-out frames for one recipient."""
    return f"{_USER_CHANNEL_PREFIX}{user_id}"
