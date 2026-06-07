"""AutosaveRateLimiter unit tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from report_service.domain.autosave_rate_limit import AutosaveRateLimiter

pytestmark = pytest.mark.asyncio


async def test_first_request_allowed():
    rl = AutosaveRateLimiter(min_interval_s=5.0)
    allowed, _ = await rl.check_and_record(report_id=uuid4())
    assert allowed


async def test_second_request_inside_window_blocked():
    rl = AutosaveRateLimiter(min_interval_s=5.0)
    rid = uuid4()
    allowed1, _ = await rl.check_and_record(report_id=rid)
    allowed2, retry = await rl.check_and_record(report_id=rid)
    assert allowed1
    assert not allowed2
    assert retry >= 1


async def test_isolated_per_report_id():
    rl = AutosaveRateLimiter(min_interval_s=5.0)
    a = uuid4()
    b = uuid4()
    allowed_a, _ = await rl.check_and_record(report_id=a)
    allowed_b, _ = await rl.check_and_record(report_id=b)
    assert allowed_a
    assert allowed_b


async def test_reset_clears_slot():
    rl = AutosaveRateLimiter(min_interval_s=5.0)
    rid = uuid4()
    await rl.check_and_record(report_id=rid)
    rl.reset(rid)
    allowed, _ = await rl.check_and_record(report_id=rid)
    assert allowed
