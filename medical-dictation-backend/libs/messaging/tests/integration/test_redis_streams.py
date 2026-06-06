"""Integration tests for the Redis Streams producer/consumer pair.

Skipped unless ``RUN_REDIS_INTEGRATION=1`` and a Redis instance is
reachable (Compose default: localhost:6379).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from redis.asyncio import Redis

from messaging import (
    RedisStreamsConsumer,
    RedisStreamsProducer,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REDIS_INTEGRATION") != "1",
    reason="set RUN_REDIS_INTEGRATION=1 to run; needs a live Redis",
)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
async def redis_client() -> Redis:
    client = Redis.from_url(REDIS_URL, decode_responses=False)
    try:
        yield client
    finally:
        await client.aclose()


async def test_round_trip(redis_client: Redis) -> None:
    stream = f"test:asr:{uuid.uuid4().hex[:8]}"
    group = "test-workers"
    producer = RedisStreamsProducer(client=redis_client, default_stream=stream)

    async with RedisStreamsConsumer(
        client=redis_client,
        producer=producer,
        stream=stream,
        group=group,
        consumer="c1",
        block_ms=500,
    ) as consumer:
        await producer.send(value=b"hello", headers={"x": "1"})

        async for msg in consumer:
            assert msg.value == b"hello"
            assert msg.headers["x"] == "1"
            await consumer.ack(msg)
            break


async def test_unacked_message_reclaimed(redis_client: Redis) -> None:
    """If consumer A crashes after read but before ack, consumer B reclaims."""
    stream = f"test:asr:{uuid.uuid4().hex[:8]}"
    group = "test-workers"
    producer = RedisStreamsProducer(client=redis_client, default_stream=stream)

    # Phase 1: consumer A reads and "crashes" (no ack).
    async with RedisStreamsConsumer(
        client=redis_client,
        producer=producer,
        stream=stream,
        group=group,
        consumer="c-A",
        block_ms=500,
        reclaim_interval_s=0.1,  # short for the test
        reclaim_idle_ms=200,
    ) as a:
        await producer.send(value=b"reclaim-me")
        async for msg in a:
            assert msg.value == b"reclaim-me"
            break  # exit without ack-ing

    # Phase 2: consumer B reclaims after idle exceeds reclaim_idle_ms.
    async with RedisStreamsConsumer(
        client=redis_client,
        producer=producer,
        stream=stream,
        group=group,
        consumer="c-B",
        block_ms=500,
        reclaim_interval_s=0.1,
        reclaim_idle_ms=200,
    ) as b:
        await asyncio.sleep(0.5)  # let reclaim fire
        # The reclaimed message will be visible to consumer B via XREADGROUP
        # with id `>` after it is reclaimed to B.
        seen = False
        for _ in range(20):
            async for msg in b:
                if msg.value == b"reclaim-me":
                    seen = True
                    await b.ack(msg)
                break
            if seen:
                break
            await asyncio.sleep(0.1)
        assert seen, "consumer B did not reclaim consumer A's stuck message"


async def test_dlq_after_max_retries(redis_client: Redis) -> None:
    stream = f"test:asr:{uuid.uuid4().hex[:8]}"
    dlq = f"{stream}:dlq"
    group = "test-workers"
    producer = RedisStreamsProducer(client=redis_client, default_stream=stream)

    async with RedisStreamsConsumer(
        client=redis_client,
        producer=producer,
        stream=stream,
        group=group,
        consumer="c1",
        block_ms=500,
        dlq_stream=dlq,
        max_retries=3,
    ) as consumer:
        await producer.send(value=b"poison")

        async for msg in consumer:
            await consumer.fail(msg, error_kind="boom-1")
            break

        # Re-read the same message after reclaim wouldn't suit a unit-style
        # test; we manually bump attempts in the headers to simulate retries.
        # The 3rd fail() pushes to DLQ.
        attempts = 1
        while attempts < 3:
            attempts += 1
            async for msg in consumer:
                # Re-emit fail with previous attempts in headers (the consumer
                # reads attempts from the on-stream header on reclaim).
                msg.headers["x-attempts"] = str(attempts - 1)
                await consumer.fail(msg, error_kind=f"boom-{attempts}")
                break

        # Verify the DLQ has the message.
        dlq_len: int = await redis_client.xlen(dlq)
        assert dlq_len >= 1
