"""Sprint-12 notification event contract.

Shared by producers (report-service, signing-service, background jobs)
and the notification-service consumer so there is exactly one definition
of the envelope on the wire — the ``libs/asr_models`` pattern from
sprint 04.

This is a LEAF package: pure Pydantic types, no other internal lib.

Reordering or renaming a field is a BREAKING change. Bump to a
``notification_events`` v2 module and run both for a deprecation window
rather than editing in place — the same discipline the WebSocket
protocol version carries.
"""

from .enums import (
    Category,
    Channel,
    EmailMode,
    Severity,
)
from .envelope import (
    EVENT_SCHEMA_VERSION,
    NotificationEvent,
)
from .publisher import build_event, publish_event
from .streams import (
    NOTIFICATIONS_DLQ_STREAM,
    NOTIFICATIONS_GROUP,
    NOTIFICATIONS_STREAM,
    user_channel,
)

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "NOTIFICATIONS_DLQ_STREAM",
    "NOTIFICATIONS_GROUP",
    "NOTIFICATIONS_STREAM",
    "Category",
    "Channel",
    "EmailMode",
    "NotificationEvent",
    "Severity",
    "build_event",
    "publish_event",
    "user_channel",
]
