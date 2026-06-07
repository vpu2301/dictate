"""Verify the Protocols admit a stub implementation under structural typing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from messaging import ConsumerProtocol, Message, ProducerProtocol


class _StubProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes | None, bytes, dict[str, str] | None]] = []

    async def send(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.sent.append((topic, key, value, headers))

    async def flush(self) -> None:
        pass


class _StubConsumer:
    def __init__(self, messages: list[Message]) -> None:
        self._messages = messages
        self._topics: list[str] = []

    def subscribe(self, topics: list[str]) -> None:
        self._topics = topics

    async def __aiter_helper(self) -> AsyncIterator[Message]:
        for m in self._messages:
            yield m

    def __aiter__(self) -> AsyncIterator[Message]:
        return self.__aiter_helper()

    async def commit(self) -> None:
        pass


def _accepts_producer(p: ProducerProtocol) -> ProducerProtocol:
    return p


def _accepts_consumer(c: ConsumerProtocol) -> ConsumerProtocol:
    return c


def test_stub_producer_satisfies_protocol() -> None:
    p = _StubProducer()
    _accepts_producer(p)


def test_stub_consumer_satisfies_protocol() -> None:
    c = _StubConsumer([])
    _accepts_consumer(c)


def test_message_is_frozen() -> None:
    m = Message(topic="t", key=b"k", value=b"v", headers={}, timestamp_ms=0)
    with pytest.raises(AttributeError):  # FrozenInstanceError subclasses AttributeError
        m.topic = "x"  # type: ignore[misc]


def test_stub_producer_records_calls() -> None:
    p = _StubProducer()
    asyncio.run(p.send("topic-a", b"k", b"v", {"trace_id": "abc"}))
    assert p.sent == [("topic-a", b"k", b"v", {"trace_id": "abc"})]
