"""Dual-window inline rate limiter (fakeredis)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fakeredis.aioredis import FakeRedis
from generation_service.domain.rate_limit import InlineRateLimiter

pytestmark = pytest.mark.asyncio


async def test_burst_cap_enforced():
    limiter = InlineRateLimiter(FakeRedis(), burst_per_second=3, per_10s=100)
    user = uuid4()
    for _ in range(3):
        allowed, _ = await limiter.check(user_id=user)
        assert allowed
    allowed, retry_after = await limiter.check(user_id=user)
    assert not allowed
    assert retry_after == 1


async def test_sustained_cap_enforced():
    limiter = InlineRateLimiter(FakeRedis(), burst_per_second=100, per_10s=5)
    user = uuid4()
    for _ in range(5):
        allowed, _ = await limiter.check(user_id=user)
        assert allowed
    allowed, retry_after = await limiter.check(user_id=user)
    assert not allowed
    assert 0 <= retry_after <= 10


async def test_users_isolated():
    limiter = InlineRateLimiter(FakeRedis(), burst_per_second=1, per_10s=100)
    assert (await limiter.check(user_id=uuid4()))[0]
    assert (await limiter.check(user_id=uuid4()))[0]


async def test_fail_open_on_redis_error():
    class _BrokenRedis:
        async def incr(self, key):
            raise ConnectionError("redis down")

    limiter = InlineRateLimiter(_BrokenRedis(), burst_per_second=1, per_10s=1)
    allowed, retry_after = await limiter.check(user_id=uuid4())
    assert allowed and retry_after == 0
