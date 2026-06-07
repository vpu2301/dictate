"""DraftAuditBuffer aggregates per-session updates."""

from __future__ import annotations

from uuid import uuid4

import pytest

from report_service.domain.draft_audit_buffer import DraftAuditBuffer

pytestmark = pytest.mark.asyncio


async def test_records_and_flushes_aggregated():
    flushes: list[tuple] = []

    async def _flush(tenant_id, report_id, session_id, entry):
        flushes.append(
            (tenant_id, report_id, session_id, entry.autosave_count, entry.final_version_number)
        )

    buf = DraftAuditBuffer(flush_fn=_flush)
    tid = uuid4()
    rid = uuid4()
    sid = uuid4()
    actor = uuid4()
    for v in range(1, 6):
        await buf.record(
            tenant_id=tid,
            report_id=rid,
            dictation_session_id=sid,
            actor_user_id=actor,
            version_number=v,
        )
    await buf.flush_session(tenant_id=tid, report_id=rid, dictation_session_id=sid)
    assert len(flushes) == 1
    _t, _r, _s, count, last = flushes[0]
    assert count == 5
    assert last == 5


async def test_no_flush_for_unknown_session_key():
    flushes: list[tuple] = []

    async def _flush(*a):
        flushes.append(a)

    buf = DraftAuditBuffer(flush_fn=_flush)
    await buf.flush_session(tenant_id=uuid4(), report_id=uuid4(), dictation_session_id=uuid4())
    assert flushes == []


async def test_flush_all_drains_buffer():
    flushes: list[tuple] = []

    async def _flush(t, r, s, e):
        flushes.append((t, r))

    buf = DraftAuditBuffer(flush_fn=_flush)
    for _ in range(3):
        await buf.record(
            tenant_id=uuid4(),
            report_id=uuid4(),
            dictation_session_id=None,
            actor_user_id=uuid4(),
            version_number=1,
        )
    await buf.flush_all()
    assert len(flushes) == 3
