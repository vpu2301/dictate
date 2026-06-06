"""Unit tests for sprint-07 demo rate limiter."""

from __future__ import annotations

import time

import pytest

from demo.rate_limit import DemoRateLimiter, RateLimitConfig


pytestmark = pytest.mark.asyncio


@pytest.fixture
def cfg() -> RateLimitConfig:
    return RateLimitConfig(
        ip_concurrent_max=2,
        ip_minutes_per_hour=5,
        user_minutes_per_day=10,
        session_max_minutes=15,
        cooldown_after_hits=2,
        cooldown_seconds=60,
    )


async def test_begin_session_under_limit(redis, cfg):
    rl = DemoRateLimiter(redis, cfg)
    assert await rl.begin_session(ip="1.1.1.1", user_id="u1") is None
    assert await rl.begin_session(ip="1.1.1.1", user_id="u2") is None


async def test_ip_concurrent_limit_breach(redis, cfg):
    rl = DemoRateLimiter(redis, cfg)
    await rl.begin_session(ip="1.1.1.1", user_id="u1")
    await rl.begin_session(ip="1.1.1.1", user_id="u2")
    breach = await rl.begin_session(ip="1.1.1.1", user_id="u3")
    assert breach is not None
    assert breach.kind == "ip_concurrent"


async def test_end_session_releases_slot(redis, cfg):
    rl = DemoRateLimiter(redis, cfg)
    await rl.begin_session(ip="1.1.1.1", user_id="u1")
    await rl.begin_session(ip="1.1.1.1", user_id="u2")
    await rl.end_session(ip="1.1.1.1")
    assert await rl.begin_session(ip="1.1.1.1", user_id="u3") is None


async def test_ip_minutes_per_hour_cap(redis, cfg):
    rl = DemoRateLimiter(redis, cfg)
    await rl.begin_session(ip="2.2.2.2", user_id="u1")
    # 5 minutes/h cap; charge 5.5 → breach.
    breach = await rl.charge_minutes(ip="2.2.2.2", user_id="u1", minutes=5.5)
    assert breach is not None
    assert breach.kind == "ip_minutes"


async def test_user_minutes_per_day_cap(redis, cfg):
    rl = DemoRateLimiter(redis, cfg)
    await rl.begin_session(ip="3.3.3.3", user_id="u1")
    breach = await rl.charge_minutes(ip="3.3.3.3", user_id="u1", minutes=11.0)
    # IP cap (5) fires first.
    assert breach is not None
    assert breach.kind in ("ip_minutes", "user_minutes")


async def test_session_too_long(redis, cfg):
    rl = DemoRateLimiter(redis, cfg)
    started = time.time() - cfg.session_max_minutes * 60 - 1
    breach = await rl.session_too_long(started_at_unix=started)
    assert breach is not None
    assert breach.kind == "session_duration"


async def test_session_not_too_long(redis, cfg):
    rl = DemoRateLimiter(redis, cfg)
    assert await rl.session_too_long(started_at_unix=time.time() - 10) is None


async def test_cooldown_engages_after_repeated_hits(redis, cfg):
    rl = DemoRateLimiter(redis, cfg)
    ip = "4.4.4.4"
    # Trigger ip_concurrent breaches repeatedly to drive hit counter.
    await rl.begin_session(ip=ip, user_id="u1")
    await rl.begin_session(ip=ip, user_id="u2")
    for _ in range(3):
        await rl.begin_session(ip=ip, user_id="ux")
    breach = await rl.begin_session(ip=ip, user_id="uy")
    assert breach is not None
    assert "cooldown" in breach.detail or breach.kind == "ip_concurrent"


async def test_fail_open_when_redis_broken(redis, cfg, monkeypatch):
    rl = DemoRateLimiter(redis, cfg)

    async def boom(*args, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis, "incr", boom)
    # Should NOT raise, should return None (fail-open).
    assert await rl.begin_session(ip="5.5.5.5", user_id="u1") is None
