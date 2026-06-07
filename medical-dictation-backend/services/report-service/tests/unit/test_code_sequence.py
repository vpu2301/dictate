"""Code generator unit tests (advisory lock stub)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from report_service.domain.code_sequence import _advisory_lock_key, next_code

# Async tests get the marker explicitly so the module works under
# pytest-asyncio modes other than auto. Sync tests at the bottom are
# left unmarked.
_aio = pytest.mark.asyncio


class StubConn:
    def __init__(self, counters: dict[tuple, int] | None = None) -> None:
        self.counters = counters or {}
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        return "OK"

    async def fetchval(self, sql: str, *args):
        self.executed.append((sql, args))
        tenant_id, year = args
        key = (tenant_id, year)
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]


@_aio
async def test_first_code_is_00001_for_new_year():
    conn = StubConn()
    code = await next_code(
        conn,
        tenant_id=uuid4(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert code == "REP-2026-00001"


@_aio
async def test_subsequent_codes_increment():
    conn = StubConn()
    tid = uuid4()
    now = datetime(2026, 5, 13, tzinfo=UTC)
    a = await next_code(conn, tenant_id=tid, now=now)
    b = await next_code(conn, tenant_id=tid, now=now)
    c = await next_code(conn, tenant_id=tid, now=now)
    assert (a, b, c) == ("REP-2026-00001", "REP-2026-00002", "REP-2026-00003")


@_aio
async def test_counter_isolated_per_year():
    conn = StubConn()
    tid = uuid4()
    a = await next_code(conn, tenant_id=tid, now=datetime(2026, 12, 31, tzinfo=UTC))
    b = await next_code(conn, tenant_id=tid, now=datetime(2027, 1, 1, tzinfo=UTC))
    assert a == "REP-2026-00001"
    assert b == "REP-2027-00001"


@_aio
async def test_counter_isolated_per_tenant():
    conn = StubConn()
    t1 = uuid4()
    t2 = uuid4()
    now = datetime(2026, 5, 13, tzinfo=UTC)
    a = await next_code(conn, tenant_id=t1, now=now)
    b = await next_code(conn, tenant_id=t2, now=now)
    assert a == "REP-2026-00001"
    assert b == "REP-2026-00001"


def test_advisory_lock_key_stable_across_invocations():
    tid = uuid4()
    assert _advisory_lock_key(tid) == _advisory_lock_key(tid)


def test_advisory_lock_key_fits_int8():
    key = _advisory_lock_key(uuid4())
    assert -(1 << 63) <= key < (1 << 63)
