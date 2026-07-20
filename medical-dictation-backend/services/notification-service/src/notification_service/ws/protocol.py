"""The `medical-notifications.v1` wire protocol.

Text frames only, JSON, a discriminated union on `type`, every model
`extra="forbid"`. The subprotocol string IS the version: a client that
does not offer it is refused at upgrade rather than served frames it may
not understand (ADR-0012 lineage, dictation-service precedent).

`docs/api/notifications-ws-v1.md` is generated from these models and is
the byte-for-byte frontend contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

SUBPROTOCOL: Final = "medical-notifications.v1"


class _Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── server → client ─────────────────────────────────────────────────


class NotificationPayload(_Frame):
    """The subset of a notification row that is safe to push.

    Mirrors the REST feed item exactly so a client can render a pushed
    frame and a fetched page with one code path.
    """

    id: UUID
    category: str
    title: str
    body_text: str
    deep_link: str
    resource_type: str
    resource_id: UUID | None
    severity: str
    created_at: datetime
    read_at: datetime | None = None


class ConnectedFrame(_Frame):
    type: Literal["connected"] = "connected"
    subprotocol: str = SUBPROTOCOL
    unread_count: int


class NotificationFrame(_Frame):
    type: Literal["notification"] = "notification"
    notification: NotificationPayload
    unread_count: int


class UnreadCountFrame(_Frame):
    type: Literal["unread_count"] = "unread_count"
    unread_count: int


class ReadAckFrame(_Frame):
    type: Literal["read_ack"] = "read_ack"
    notification_id: UUID
    unread_count: int


class PongFrame(_Frame):
    type: Literal["pong"] = "pong"


class ErrorFrame(_Frame):
    type: Literal["error"] = "error"
    code: str
    detail: str = ""


ServerFrame = Annotated[
    ConnectedFrame
    | NotificationFrame
    | UnreadCountFrame
    | ReadAckFrame
    | PongFrame
    | ErrorFrame,
    Field(discriminator="type"),
]


# ── client → server ─────────────────────────────────────────────────


class MarkReadCommand(_Frame):
    type: Literal["mark_read"]
    notification_id: UUID


class PingCommand(_Frame):
    type: Literal["ping"]


ClientCommand = Annotated[
    MarkReadCommand | PingCommand,
    Field(discriminator="type"),
]


_CLIENT_ADAPTER: Final[TypeAdapter[ClientCommand]] = TypeAdapter(ClientCommand)


def parse_client_frame(raw: str | bytes) -> ClientCommand:
    """Validate one inbound frame.

    Raises ``pydantic.ValidationError``; the handler maps that to an
    `error` frame rather than dropping the connection, so a client bug
    is visible to the client instead of looking like a network fault.
    """
    return _CLIENT_ADAPTER.validate_json(raw)


def dump_frame(frame: BaseModel) -> str:
    """Serialise a server frame. `mode="json"` so UUIDs/datetimes render."""
    return frame.model_dump_json()
