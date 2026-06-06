"""Producer / consumer Protocols and the wire ``Message`` type.

These are pure ``typing.Protocol`` definitions. They have no runtime
behaviour; they exist so services can depend on a shape, not on a
specific transport, while we negotiate transport choices across sprints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass(frozen=True, slots=True)
class Message:
    """Wire message common to every transport."""

    topic: str
    key: bytes | None
    value: bytes
    headers: dict[str, str]
    timestamp_ms: int
    partition: int | None = None
    offset: int | None = None


class ProducerProtocol(Protocol):
    async def send(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    async def flush(self) -> None: ...


class ConsumerProtocol(Protocol):
    def subscribe(self, topics: list[str]) -> None: ...

    def __aiter__(self) -> AsyncIterator[Message]: ...

    async def commit(self) -> None: ...
