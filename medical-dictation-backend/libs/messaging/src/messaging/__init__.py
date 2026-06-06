"""libs/messaging — Protocol contracts + Redis Streams concrete impl.

Sprint 03 ships the Redis Streams producer/consumer pair. Sprint 14 will
add a Kafka pair that satisfies the same Protocols.

Public surface:

- :class:`Message`              — wire-stable frozen dataclass.
- :class:`ProducerProtocol`     — typing.Protocol for any producer.
- :class:`ConsumerProtocol`     — typing.Protocol for any consumer.
- :class:`RedisStreamsProducer` — Redis Streams ``XADD`` producer.
- :class:`RedisStreamsConsumer` — Redis Streams consumer with
                                  ``XREADGROUP`` + ``XAUTOCLAIM`` reclaim
                                  + DLQ-on-retries policy.

The redis-streams classes import ``redis.asyncio`` at module import; in
environments where ``redis`` isn't installed (e.g., the sprint-01/02
test rigs that only use the Protocols), accessing those names raises
``ImportError`` with a clear message.
"""

from .protocols import ConsumerProtocol, Message, ProducerProtocol

try:
    from .redis_streams import (
        DEFAULT_DLQ_SUFFIX,
        RedisStreamsConsumer,
        RedisStreamsProducer,
    )

    _REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover  — `redis` not installed
    _REDIS_AVAILABLE = False
    DEFAULT_DLQ_SUFFIX = ":dlq"  # type: ignore[assignment]

    class _MissingRedis:
        def __init__(self, *_a: object, **_kw: object) -> None:
            raise ImportError(
                "redis is not installed; pip install redis>=5.0 to use "
                "RedisStreamsProducer / RedisStreamsConsumer."
            )

    RedisStreamsProducer = _MissingRedis  # type: ignore[assignment,misc]
    RedisStreamsConsumer = _MissingRedis  # type: ignore[assignment,misc]


__all__ = [
    "ConsumerProtocol",
    "DEFAULT_DLQ_SUFFIX",
    "Message",
    "ProducerProtocol",
    "RedisStreamsConsumer",
    "RedisStreamsProducer",
]
