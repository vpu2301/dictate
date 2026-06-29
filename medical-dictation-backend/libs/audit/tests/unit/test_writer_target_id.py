"""Regression: AuditWriter must coerce a UUID target_id to str.

The ``audit.events.target_id`` column is TEXT and the JCS canonicalizer only
serializes JSON-native types, so a caller passing a ``uuid.UUID`` (e.g. the
``asyncpg.pgproto.UUID`` returned by ``fetchval``) must be stringified before
both the canonical hash record and the INSERT bind. This is exercised with a
fake pool/connection so it needs no live database.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from audit.writer import AuditWriter


class _FakeTxn:
    async def __aenter__(self) -> "_FakeTxn":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self, **_: object) -> _FakeTxn:
        return _FakeTxn()

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, args))
        return "INSERT 0 1"

    async def fetchrow(self, *_: object) -> None:
        return None  # no prior event → genesis chain head


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


@pytest.mark.asyncio
async def test_write_event_stringifies_uuid_target_id() -> None:
    conn = _FakeConn()
    writer = AuditWriter(_FakePool(conn))  # type: ignore[arg-type]
    report_id = uuid.uuid4()

    await writer.write_event(
        tenant_id=uuid.uuid4(),
        kind="report.created",
        actor_sub=uuid.uuid4(),
        actor_role="clinician",
        target_kind="report",
        target_id=report_id,  # a UUID, not a str
        payload={"code": "R-1"},
    )

    insert = next(c for c in conn.calls if "INSERT INTO audit.events" in c[0])
    # VALUES order: tenant_id, seq, created_at, actor_sub, actor_role, kind,
    # target_kind, target_id, … → target_id is positional arg index 7.
    target_id_bind = insert[1][7]
    assert target_id_bind == str(report_id)
    assert isinstance(target_id_bind, str)
