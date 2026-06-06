"""PublicVerifyRateLimiter — fakeredis-backed."""

from __future__ import annotations

import pytest
import pytest_asyncio
import fakeredis.aioredis

from signing_service.rate_limit import PublicVerifyRateLimiter


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def test_under_limit_allows(redis):
    rl = PublicVerifyRateLimiter(redis, per_minute=60)
    for _ in range(10):
        allowed, _ = await rl.check(ip="1.1.1.1")
        assert allowed


async def test_above_limit_429(redis):
    rl = PublicVerifyRateLimiter(redis, per_minute=3)
    for _ in range(3):
        allowed, _ = await rl.check(ip="1.1.1.1")
        assert allowed
    allowed, retry = await rl.check(ip="1.1.1.1")
    assert not allowed
    assert retry > 0


async def test_isolated_per_ip(redis):
    rl = PublicVerifyRateLimiter(redis, per_minute=2)
    for _ in range(2):
        assert (await rl.check(ip="1.1.1.1"))[0]
    for _ in range(2):
        assert (await rl.check(ip="2.2.2.2"))[0]


async def test_fail_open_on_redis_error(redis, monkeypatch):
    rl = PublicVerifyRateLimiter(redis, per_minute=2)

    async def boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis, "incr", boom)
    allowed, retry = await rl.check(ip="3.3.3.3")
    assert allowed
    assert retry == 0
