"""Redis Streams producer/consumer with at-least-once + DLQ semantics.

Why Redis Streams (and not Kafka) for ASR jobs (sprint 03)?
  - Lower latency: workers want to pick up jobs in tens of milliseconds.
  - We don't need partition-level ordering across tenants here.
  - We already run Redis for caching; one fewer broker to operate.
See ADR-0010.

Consumer-group lifecycle handled here:

1. ``XGROUP CREATE`` on first use (idempotent; tolerates BUSYGROUP).
2. ``XREADGROUP`` blocks up to 5 s for new messages.
3. Stuck-consumer recovery via ``XAUTOCLAIM`` every 60 s.
4. Per-message retry counter in the headers; max 3 retries by default,
   then the message is XADD'd to a sibling DLQ stream and XACK'd.
5. Consumer crash: messages remain in the pending-entries list until
   reclaim, then re-delivered to another consumer.

Idempotency is the *caller's* responsibility — workers consult the
``transcription_jobs`` row before doing work and skip if it's already
``complete``/``failed``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .protocols import Message

logger = logging.getLogger(__name__)

DEFAULT_DLQ_SUFFIX: Final = ":dlq"
DEFAULT_BLOCK_MS: Final = 5_000
DEFAULT_RECLAIM_IDLE_MS: Final = 60_000
DEFAULT_RECLAIM_INTERVAL_S: Final = 60.0
DEFAULT_MAX_RETRIES: Final = 3
HEADER_ATTEMPTS_KEY: Final = "x-attempts"


@dataclass(slots=True)
class _PendingMessage:
    message_id: bytes
    fields: dict[bytes, bytes]


class RedisStreamsProducer:
    """``XADD`` producer for a single primary stream + optional override.

    All sprint-03 callers use the same primary stream; the ``send``
    method allows passing an explicit ``topic`` so a single producer
    instance can also serve the DLQ path.
    """

    def __init__(
        self,
        *,
        client: Redis,
        default_stream: str,
        maxlen: int | None = 100_000,
    ) -> None:
        self._client = client
        self._default_stream = default_stream
        self._maxlen = maxlen

    async def send(
        self,
        topic: str | None = None,
        key: bytes | None = None,
        value: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> str:
        stream = topic or self._default_stream
        fields: dict[bytes, bytes] = {b"value": value}
        if key is not None:
            fields[b"key"] = key
        for h_name, h_value in (headers or {}).items():
            fields[f"h-{h_name}".encode()] = h_value.encode("utf-8")

        kwargs: dict[str, Any] = {}
        if self._maxlen is not None:
            kwargs["maxlen"] = self._maxlen
            kwargs["approximate"] = True

        msg_id_raw: Any = await self._client.xadd(stream, fields, **kwargs)  # type: ignore[arg-type]
        msg_id = msg_id_raw.decode("utf-8") if isinstance(msg_id_raw, bytes) else str(msg_id_raw)
        logger.debug(
            "redis_streams.xadd",
            extra={"stream": stream, "message_id": msg_id, "key_set": key is not None},
        )
        return msg_id

    async def flush(self) -> None:
        # XADD is synchronous from the client's perspective — nothing to flush.
        return None

    async def aclose(self) -> None:
        # Connection is shared with the readiness-probe client; do not close
        # the underlying Redis here. Symmetry method only.
        return None


class RedisStreamsConsumer:
    """``XREADGROUP`` consumer with reclaim + DLQ semantics.

    Iteration protocol::

        async with RedisStreamsConsumer(...) as consumer:
            async for message in consumer:
                try:
                    await handle(message)
                    await consumer.ack(message)
                except RetryableError:
                    # Don't ack — message stays in the pending list and is
                    # reclaimed by XAUTOCLAIM after the idle interval.
                    pass
                except Exception:
                    await consumer.fail(message, error_kind="...")
                    # Bumps attempts; DLQ on max-retries.

    The ``__aiter__`` loop transparently runs the reclaim task in the
    background so callers don't have to plumb it themselves.
    """

    def __init__(
        self,
        *,
        client: Redis,
        producer: RedisStreamsProducer,
        stream: str,
        group: str,
        consumer: str,
        dlq_stream: str | None = None,
        block_ms: int = DEFAULT_BLOCK_MS,
        reclaim_idle_ms: int = DEFAULT_RECLAIM_IDLE_MS,
        reclaim_interval_s: float = DEFAULT_RECLAIM_INTERVAL_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._client = client
        self._producer = producer
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._dlq_stream = dlq_stream or f"{stream}{DEFAULT_DLQ_SUFFIX}"
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._reclaim_interval_s = reclaim_interval_s
        self._max_retries = max_retries
        self._stop = asyncio.Event()
        self._reclaim_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> RedisStreamsConsumer:
        await self._ensure_group()
        self._reclaim_task = asyncio.create_task(self._reclaim_loop())
        return self

    async def __aexit__(self, *_: Any) -> None:
        self._stop.set()
        if self._reclaim_task is not None:
            self._reclaim_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reclaim_task

    async def _ensure_group(self) -> None:
        try:
            # `$` = only messages produced after this group is created.
            # `mkstream=True` creates the stream if it doesn't exist yet.
            await self._client.xgroup_create(self._stream, self._group, id="$", mkstream=True)
            logger.info(
                "redis_streams.group_created",
                extra={"stream": self._stream, "group": self._group},
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
            # Group already exists — common case on restart.

    def subscribe(self, topics: list[str]) -> None:
        # Single-stream consumer; the topic was supplied at construction.
        # Implemented to satisfy ConsumerProtocol; would raise if multiple
        # topics are given.
        if list(topics) != [self._stream]:
            raise ValueError(
                f"RedisStreamsConsumer is bound to {self._stream!r}; "
                f"subscribe({topics!r}) does not match."
            )

    async def __aiter__(self) -> AsyncIterator[Message]:
        while not self._stop.is_set():
            try:
                resp = await self._client.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer,
                    streams={self._stream: ">"},
                    count=1,
                    block=self._block_ms,
                )
            except RedisTimeoutError:
                # The blocking XREADGROUP hit its BLOCK deadline with no new
                # messages. redis-py >=8 raises here instead of returning
                # None (older versions returned an empty reply). This is the
                # idle path, not an error — keep polling.
                continue
            except ResponseError as exc:
                logger.warning(
                    "redis_streams.xreadgroup_error",
                    extra={"error": str(exc), "stream": self._stream},
                )
                await asyncio.sleep(1)
                continue

            if not resp:
                continue
            for _stream_name, entries in resp:
                for msg_id_raw, fields in entries:
                    yield _to_message(self._stream, msg_id_raw, fields)

    async def commit(self) -> None:
        # libs/messaging.ConsumerProtocol's ``commit`` is a no-op for
        # Redis Streams: acks happen per-message via ``ack``. Kept to
        # satisfy the Protocol.
        return None

    async def ack(self, message: Message) -> None:
        if message.offset is None:
            return
        # `offset` carries the Redis Streams message id (str) on our impl.
        msg_id = message.headers.get("_id")
        if msg_id is None:
            return
        await self._client.xack(self._stream, self._group, msg_id)

    async def fail(self, message: Message, *, error_kind: str) -> None:
        """Increment the retry counter; on max retries push to DLQ + ack."""
        msg_id = message.headers.get("_id")
        if msg_id is None:
            return
        attempts = int(message.headers.get(HEADER_ATTEMPTS_KEY, "0")) + 1
        if attempts >= self._max_retries:
            await self._producer.send(
                topic=self._dlq_stream,
                key=message.key,
                value=message.value,
                headers={
                    **{k: v for k, v in message.headers.items() if not k.startswith("_")},
                    HEADER_ATTEMPTS_KEY: str(attempts),
                    "x-final-error-kind": error_kind,
                    "x-original-stream": self._stream,
                    "x-original-message-id": msg_id,
                },
            )
            await self._client.xack(self._stream, self._group, msg_id)
            logger.warning(
                "redis_streams.dlq",
                extra={
                    "stream": self._stream,
                    "dlq": self._dlq_stream,
                    "message_id": msg_id,
                    "error_kind": error_kind,
                    "attempts": attempts,
                },
            )
            return
        # Not at the cap yet — leave the message in the pending entries
        # list. Reclaim or a subsequent XREADGROUP after consumer restart
        # will re-deliver it.
        logger.info(
            "redis_streams.retry",
            extra={
                "stream": self._stream,
                "message_id": msg_id,
                "attempts": attempts,
                "max_retries": self._max_retries,
            },
        )

    async def _reclaim_loop(self) -> None:
        """Background reclaim of stuck pending messages.

        ``XAUTOCLAIM`` reassigns ownership of any message that has been
        idle in the pending list for ``reclaim_idle_ms``. Idle messages
        usually indicate a crashed consumer that didn't ack.
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._reclaim_interval_s)
                return  # stop set
            except TimeoutError:
                pass
            try:
                _cursor, claimed, _deleted = await self._client.xautoclaim(
                    name=self._stream,
                    groupname=self._group,
                    consumername=self._consumer,
                    min_idle_time=self._reclaim_idle_ms,
                    count=10,
                )
                if claimed:
                    logger.info(
                        "redis_streams.reclaimed",
                        extra={
                            "stream": self._stream,
                            "count": len(claimed),
                            "as_consumer": self._consumer,
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "redis_streams.autoclaim_error",
                    extra={
                        "error": str(exc),
                        "error_class": type(exc).__name__,
                        "stream": self._stream,
                    },
                )


def _to_message(
    stream: str,
    msg_id_raw: bytes,
    fields: dict[bytes, bytes],
) -> Message:
    msg_id = msg_id_raw.decode("utf-8") if isinstance(msg_id_raw, bytes) else str(msg_id_raw)
    value = fields.get(b"value", b"")
    key = fields.get(b"key")
    headers: dict[str, str] = {"_id": msg_id}
    for k, v in fields.items():
        if k in (b"value", b"key"):
            continue
        k_str = k.decode("utf-8")
        if k_str.startswith("h-"):
            headers[k_str[2:]] = v.decode("utf-8")
        else:
            headers[k_str] = v.decode("utf-8")
    # Redis stream IDs encode ms timestamp + sequence.
    try:
        ts_ms = int(msg_id.split("-", 1)[0])
    except ValueError:
        ts_ms = int(time.time() * 1000)
    return Message(
        topic=stream,
        key=key,
        value=value,
        headers=headers,
        timestamp_ms=ts_ms,
        partition=None,
        offset=None,
    )
