"""Delivery-semantics regression tests for RedisStreamsConsumer.

These run against fakeredis on every `make test` — deliberately NOT
behind RUN_REDIS_INTEGRATION. The three defects covered here all
survived because the only coverage was an env-gated integration suite
that never ran in CI:

  * ``ack()`` guarded on ``Message.offset``, which ``_to_message`` always
    sets to None, so XACK never fired and the PEL grew without bound.
  * the retry counter was read from the message headers, but a failed
    entry is re-delivered with its original fields, so the count always
    read back as 0 and the DLQ cap was never reached.
  * ``_reclaim_loop`` logged the entries XAUTOCLAIM returned but never
    yielded them, and XREADGROUP '>' cannot return them either — so a
    crashed consumer's in-flight messages were stranded.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fakeredis.aioredis import FakeRedis

from messaging import Message, RedisStreamsConsumer, RedisStreamsProducer


@pytest.fixture
async def redis_client() -> FakeRedis:
    client = FakeRedis(decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()


def _names() -> tuple[str, str, str]:
    stream = f"test:notif:{uuid.uuid4().hex[:8]}"
    return stream, f"{stream}:dlq", "test-group"


async def _pending_count(client: FakeRedis, stream: str, group: str) -> int:
    summary = await client.xpending(stream, group)
    # redis-py returns a dict for the summary form.
    return int(summary["pending"] if isinstance(summary, dict) else summary[0])


async def test_ack_removes_entry_from_pending(redis_client: FakeRedis) -> None:
    """The regression: ack() used to no-op because offset is always None."""
    stream, dlq, group = _names()
    producer = RedisStreamsProducer(client=redis_client, default_stream=stream)

    async with RedisStreamsConsumer(
        client=redis_client,
        producer=producer,
        stream=stream,
        group=group,
        consumer="c1",
        dlq_stream=dlq,
        block_ms=50,
    ) as consumer:
        await producer.send(value=b"payload")

        async for msg in consumer:
            assert msg.value == b"payload"
            await consumer.ack(msg)
            break

        assert await _pending_count(redis_client, stream, group) == 0


async def test_dlq_after_max_retries_without_header_help(
    redis_client: FakeRedis,
) -> None:
    """fail() must reach the cap on its own — no hand-injected x-attempts."""
    stream, dlq, group = _names()
    producer = RedisStreamsProducer(client=redis_client, default_stream=stream)

    async with RedisStreamsConsumer(
        client=redis_client,
        producer=producer,
        stream=stream,
        group=group,
        consumer="c1",
        dlq_stream=dlq,
        block_ms=50,
        max_retries=3,
    ) as consumer:
        await producer.send(value=b"poison")

        first: Message | None = None
        async for msg in consumer:
            first = msg
            break
        assert first is not None

        # Re-delivery hands back a Message rebuilt from the SAME stream
        # fields, so failing the same object three times is exactly the
        # retry sequence. Under the old header-borne counter this read
        # back as attempts=1 every time and never reached the cap.
        # The return value is the "this work is now permanently off the
        # stream" signal: a caller that keeps its own record of the job
        # (asr-worker keeps a transcription_jobs row) has no other way to
        # learn the queue gave up, and the record would wait forever.
        assert await consumer.fail(first, error_kind="boom-1") is False
        assert await redis_client.xlen(dlq) == 0

        assert await consumer.fail(first, error_kind="boom-2") is False
        assert await redis_client.xlen(dlq) == 0

        assert await consumer.fail(first, error_kind="boom-3") is True
        assert await redis_client.xlen(dlq) == 1
        assert await _pending_count(redis_client, stream, group) == 0


async def test_reclaimed_entries_are_redelivered(redis_client: FakeRedis) -> None:
    """A crashed consumer's in-flight message must reach a live consumer."""
    stream, dlq, group = _names()
    producer = RedisStreamsProducer(client=redis_client, default_stream=stream)

    # Consumer A reads and "crashes" without acking.
    async with RedisStreamsConsumer(
        client=redis_client,
        producer=producer,
        stream=stream,
        group=group,
        consumer="crashed",
        dlq_stream=dlq,
        block_ms=50,
        reclaim_interval_s=60.0,
        reclaim_idle_ms=0,
    ) as a:
        await producer.send(value=b"orphan")
        async for msg in a:
            assert msg.value == b"orphan"
            break  # no ack — entry stays in the PEL

    assert await _pending_count(redis_client, stream, group) == 1

    # Consumer B must pick it up via XAUTOCLAIM and see it in the iterator.
    async with RedisStreamsConsumer(
        client=redis_client,
        producer=producer,
        stream=stream,
        group=group,
        consumer="rescuer",
        dlq_stream=dlq,
        block_ms=50,
        reclaim_interval_s=60.0,  # driven explicitly below, not by the timer
        reclaim_idle_ms=0,
    ) as b:
        assert await b.reclaim_once() == 1

        # The claimed entry must come back out of the iterator. Drive one
        # step of the generator rather than an `async for` + `break`, which
        # would leave the generator suspended for the GC to finalise.
        it = b.__aiter__()
        try:
            rescued = await asyncio.wait_for(anext(it), timeout=5.0)
        finally:
            await it.aclose()

        assert rescued.value == b"orphan", "reclaimed entry never reached the iterator"
        await b.ack(rescued)
        assert await _pending_count(redis_client, stream, group) == 0
