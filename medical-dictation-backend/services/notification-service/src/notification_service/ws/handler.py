"""Per-connection session loop + the pub/sub → local-socket dispatch."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from db import tenant_connection

from ..domain import repository as repo
from .protocol import (
    SUBPROTOCOL,
    ConnectedFrame,
    ErrorFrame,
    MarkReadCommand,
    NotificationFrame,
    NotificationPayload,
    PingCommand,
    PongFrame,
    ReadAckFrame,
    UnreadCountFrame,
    parse_client_frame,
)
from .upgrade import UpgradeContext

logger = logging.getLogger(__name__)


async def run_session(
    websocket: WebSocket, *, upgrade: UpgradeContext, state: Any
) -> None:
    """Serve one authenticated socket until the peer goes away."""
    claims = upgrade.claims
    user_id: UUID = claims.sub
    tenant_id: UUID = claims.tid

    # Echo the negotiated subprotocol — a client that offered it expects
    # it back, and browsers fail the connection if it is absent.
    await websocket.accept(subprotocol=SUBPROTOCOL)
    await state.socket_registry.add(user_id, websocket)

    try:
        async with tenant_connection(state.app_pool, tenant_id) as conn:
            count = await repo.unread_count(conn, user_id=user_id)
        await websocket.send_text(ConnectedFrame(unread_count=count).model_dump_json())

        while True:
            raw = await websocket.receive_text()
            await _handle_command(
                raw, websocket=websocket, user_id=user_id, tenant_id=tenant_id, state=state
            )
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("ws.session_error", extra={"error": str(exc)})
    finally:
        await state.socket_registry.remove(user_id, websocket)


async def _handle_command(
    raw: str, *, websocket: WebSocket, user_id: UUID, tenant_id: UUID, state: Any
) -> None:
    try:
        command = parse_client_frame(raw)
    except ValidationError:
        # A malformed frame is a client bug. Reporting it beats closing
        # the socket, which would look like a network fault.
        await websocket.send_text(
            ErrorFrame(code="bad_frame", detail="frame failed validation").model_dump_json()
        )
        return

    if isinstance(command, PingCommand):
        await websocket.send_text(PongFrame().model_dump_json())
        return

    if isinstance(command, MarkReadCommand):
        async with tenant_connection(state.app_pool, tenant_id) as conn:
            # RLS plus the recipient predicate mean a user cannot mark
            # someone else's notification read even by guessing its id.
            await repo.mark_read(
                conn, user_id=user_id, notification_id=command.notification_id
            )
            count = await repo.unread_count(conn, user_id=user_id)
        await websocket.send_text(
            ReadAckFrame(
                notification_id=command.notification_id, unread_count=count
            ).model_dump_json()
        )


def make_fanout_handler(state: Any):
    """Build the callback the pub/sub bridge invokes on every frame.

    The published frame carries ids only, so this re-reads the row under
    the recipient's own tenant scope. That keeps pub/sub — which has no
    tenant isolation — from ever carrying notification content.
    """

    async def handle(payload: dict[str, Any]) -> None:
        raw_user = payload.get("recipient_user_id")
        raw_tenant = payload.get("tenant_id")
        if not raw_user or not raw_tenant:
            return

        user_id = UUID(raw_user)
        tenant_id = UUID(raw_tenant)

        # Fast path: if this worker holds no socket for that user, the
        # frame is not ours. Skip before touching the database — most
        # frames on a multi-worker deployment land here.
        if not await state.socket_registry.sockets_for(user_id):
            return

        async with tenant_connection(state.app_pool, tenant_id) as conn:
            count = await repo.unread_count(conn, user_id=user_id)
            row = None
            raw_notification = payload.get("notification_id")
            if payload.get("kind") == "notification" and raw_notification:
                row = await conn.fetchrow(
                    "SELECT id, category, title, body_text, deep_link, resource_type, "
                    "       resource_id, severity, read_at, created_at "
                    "  FROM notifications WHERE id = $1 AND recipient_user_id = $2",
                    UUID(raw_notification),
                    user_id,
                )

        if row is not None:
            frame = NotificationFrame(
                notification=NotificationPayload(**dict(row)), unread_count=count
            )
        else:
            frame = UnreadCountFrame(unread_count=count)

        await state.socket_registry.send_to_user(user_id, frame.model_dump_json())

    return handle
