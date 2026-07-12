"""Telemetry buffer — bounded, batched, sheds instead of blocking."""

from __future__ import annotations

import asyncio

import pytest
from autocomplete_service.telemetry_buffer import MAX_BUFFER_FACTOR, TelemetryBuffer

pytestmark = pytest.mark.asyncio


class FakePool:
    """Records batches; can be told to fail N acquisitions."""

    def __init__(self, fail_times: int = 0) -> None:
        self.batches: list[list[tuple]] = []
        self.fail_times = fail_times
        self.acquires = 0

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                pool.acquires += 1
                if pool.fail_times > 0:
                    pool.fail_times -= 1
                    raise ConnectionError("db down")
                return _Conn()

            async def __aexit__(self, *a):
                return False

        class _Conn:
            async def executemany(self, _sql, rows):
                pool.batches.append(list(rows))

        return _Ctx()


class Dropped:
    def __init__(self) -> None:
        self.total = 0
        self.reasons: list[str] = []

    def add(self, n, attrs=None):
        self.total += n
        self.reasons.append((attrs or {}).get("reason"))


def _buf(pool, batch=5, interval=999.0, dropped=None):
    return TelemetryBuffer(
        pool, flush_interval_s=interval, flush_batch=batch, dropped_metric=dropped
    )


async def test_flush_at_batch_threshold():
    pool = FakePool()
    buf = _buf(pool, batch=5)
    for i in range(5):
        buf.append((i,))
    await asyncio.sleep(0)  # let the create_task'd flush run
    await buf._flush_locked()
    assert sum(len(b) for b in pool.batches) == 5


async def test_interval_flush():
    pool = FakePool()
    buf = TelemetryBuffer(pool, flush_interval_s=0.01, flush_batch=100)
    buf.start()
    buf.append(("row",))
    await asyncio.sleep(0.05)
    await buf.stop()
    assert sum(len(b) for b in pool.batches) == 1


async def test_shutdown_flushes_remainder():
    pool = FakePool()
    buf = _buf(pool, batch=100)
    buf.append(("row1",))
    buf.append(("row2",))
    await buf.stop()
    assert sum(len(b) for b in pool.batches) == 2


async def test_overflow_sheds_oldest_with_counter():
    pool = FakePool(fail_times=10_000)  # DB permanently down → nothing drains
    dropped = Dropped()
    buf = _buf(pool, batch=10, dropped=dropped)
    cap = MAX_BUFFER_FACTOR * 10
    for i in range(cap + 25):
        buf.append((i,))
    assert len(buf._rows) <= cap
    assert dropped.total >= 25
    assert "buffer_overflow" in dropped.reasons
    # oldest were shed — the newest row is still present
    assert (cap + 24,) in buf._rows


async def test_failed_flush_retries_once_then_drops_with_counter():
    pool = FakePool(fail_times=2)  # original + retry both fail
    dropped = Dropped()
    buf = _buf(pool, batch=100, dropped=dropped)
    buf.append(("row",))
    await buf._flush_locked()
    assert pool.acquires == 2  # exactly one retry
    assert dropped.total == 1
    assert dropped.reasons[-1] == "flush_failed"
    # request path never sees the failure; a later append still works
    pool.fail_times = 0
    buf.append(("row2",))
    await buf._flush_locked()
    assert sum(len(b) for b in pool.batches) == 1
