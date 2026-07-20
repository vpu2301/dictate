"""Per-worker registry of locally-connected sockets.

Deliberately process-local and unreplicated. The cross-worker problem is
solved by pub/sub (fanout.py), not by sharing this map — a shared
registry would need cleaning up after every crashed worker, and a stale
entry there means sending to a dead socket forever.

One user may hold several sockets at once (two browser tabs), so the
value is a set, not a single connection.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Protocol
from uuid import UUID

logger = logging.getLogger(__name__)


class SendableSocket(Protocol):
    """The slice of starlette's WebSocket this module needs."""

    async def send_text(self, data: str) -> None: ...


class SocketRegistry:
    def __init__(self) -> None:
        self._by_user: dict[UUID, set[SendableSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def add(self, user_id: UUID, socket: SendableSocket) -> None:
        async with self._lock:
            self._by_user[user_id].add(socket)

    async def remove(self, user_id: UUID, socket: SendableSocket) -> None:
        async with self._lock:
            sockets = self._by_user.get(user_id)
            if sockets is None:
                return
            sockets.discard(socket)
            if not sockets:
                # Drop the empty set so the map does not grow without
                # bound across a long-lived process.
                del self._by_user[user_id]

    async def sockets_for(self, user_id: UUID) -> list[SendableSocket]:
        async with self._lock:
            return list(self._by_user.get(user_id, ()))

    async def send_to_user(self, user_id: UUID, payload: str) -> int:
        """Best-effort push. Returns how many sockets accepted the frame.

        A send failure means the peer went away between the registry
        lookup and the write — a normal race on disconnect, not an
        error. The socket is dropped and the caller carries on; the
        client will re-read state on reconnect (E5).
        """
        sent = 0
        for socket in await self.sockets_for(user_id):
            try:
                await socket.send_text(payload)
                sent += 1
            except Exception:  # noqa: BLE001
                await self.remove(user_id, socket)
        return sent

    def connected_user_count(self) -> int:
        """Feeds the fan-out gauge."""
        return len(self._by_user)

    def connected_socket_count(self) -> int:
        return sum(len(s) for s in self._by_user.values())
