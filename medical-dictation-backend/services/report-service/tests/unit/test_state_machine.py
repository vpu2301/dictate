"""Unit tests for ReportStateMachine.

These tests use a stub asyncpg.Connection so we can drive the SQL paths
without a live DB. The state machine is the irreversibility hotspot —
every allowed/disallowed transition is exercised here, with concurrent
+ cross-user variants where applicable.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from report_models import ReportStatus

from report_service.domain.report_lifecycle import (
    ConcurrentTransitionError,
    IllegalTransitionError,
    NotPrimaryAuthorError,
    ReportStateMachine,
    RevertWindowExceededError,
    TransitionAction,
)


# Async tests get the marker explicitly so the module works under
# pytest-asyncio modes other than auto. Sync tests at the bottom are
# left unmarked.
_aio = pytest.mark.asyncio


class StubConn:
    """Minimal asyncpg-shaped stub used in pure unit tests.

    Driven entirely by the prepared responses queued on the instance.
    """

    def __init__(self) -> None:
        self._fetchrow_queue: list = []
        self.executed: list[tuple[str, tuple]] = []

    def push_fetchrow(self, *rows) -> None:
        self._fetchrow_queue.extend(rows)

    async def fetchrow(self, sql, *args):
        self.executed.append((sql, args))
        if not self._fetchrow_queue:
            return None
        return self._fetchrow_queue.pop(0)

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"


def _row(**kw):
    """asyncpg.Record-like dict that supports row['key'] indexing."""
    return kw


# ── Allowed transitions ─────────────────────────────────────────────


@_aio
async def test_finalize_happy_path():
    sm = ReportStateMachine()
    conn = StubConn()
    conn.push_fetchrow(_row(id=uuid4()))  # UPDATE...RETURNING success
    result = await sm.finalize(conn, report_id=uuid4())
    assert result.from_status == ReportStatus.DRAFT
    assert result.to_status == ReportStatus.FINALIZED
    assert result.action == TransitionAction.FINALIZE


@_aio
async def test_finalize_concurrent_returns_observed_state():
    sm = ReportStateMachine()
    conn = StubConn()
    # First fetchrow (UPDATE...RETURNING) returns None → no row updated.
    # Second fetchrow (SELECT status) returns the observed state.
    conn.push_fetchrow(None, _row(status="finalized"))
    with pytest.raises(ConcurrentTransitionError) as exc:
        await sm.finalize(conn, report_id=uuid4())
    assert exc.value.observed_status == ReportStatus.FINALIZED


@_aio
async def test_cancel_from_draft_allowed():
    sm = ReportStateMachine()
    conn = StubConn()
    conn.push_fetchrow(_row(id=uuid4()))
    result = await sm.cancel(
        conn, report_id=uuid4(), from_status=ReportStatus.DRAFT, reason="dup"
    )
    assert result.to_status == ReportStatus.CANCELLED


@_aio
async def test_cancel_from_signed_disallowed():
    sm = ReportStateMachine()
    conn = StubConn()
    with pytest.raises(IllegalTransitionError):
        await sm.cancel(
            conn,
            report_id=uuid4(),
            from_status=ReportStatus.SIGNED,
            reason="never",
        )


@_aio
async def test_revert_happy_path_inside_window():
    sm = ReportStateMachine()
    conn = StubConn()
    actor = uuid4()
    now = datetime.now(timezone.utc)
    conn.push_fetchrow(
        _row(
            status="finalized",
            primary_author_id=actor,
            finalized_at=now - timedelta(minutes=30),
        ),
        _row(id=uuid4()),
    )
    result = await sm.revert_to_draft(
        conn, report_id=uuid4(), actor_user_id=actor, now=now
    )
    assert result.to_status == ReportStatus.DRAFT


@_aio
async def test_revert_outside_window_rejected():
    sm = ReportStateMachine()
    conn = StubConn()
    actor = uuid4()
    now = datetime.now(timezone.utc)
    conn.push_fetchrow(
        _row(
            status="finalized",
            primary_author_id=actor,
            finalized_at=now - timedelta(hours=2),
        ),
    )
    with pytest.raises(RevertWindowExceededError):
        await sm.revert_to_draft(
            conn, report_id=uuid4(), actor_user_id=actor, now=now
        )


@_aio
async def test_revert_non_primary_author_rejected():
    sm = ReportStateMachine()
    conn = StubConn()
    actor = uuid4()
    other = uuid4()
    now = datetime.now(timezone.utc)
    conn.push_fetchrow(
        _row(
            status="finalized",
            primary_author_id=other,
            finalized_at=now - timedelta(minutes=5),
        ),
    )
    with pytest.raises(NotPrimaryAuthorError):
        await sm.revert_to_draft(
            conn, report_id=uuid4(), actor_user_id=actor, now=now
        )


@_aio
async def test_revert_from_draft_disallowed():
    sm = ReportStateMachine()
    conn = StubConn()
    actor = uuid4()
    conn.push_fetchrow(
        _row(status="draft", primary_author_id=actor, finalized_at=None),
    )
    with pytest.raises(IllegalTransitionError):
        await sm.revert_to_draft(
            conn, report_id=uuid4(), actor_user_id=actor
        )


@_aio
async def test_revert_from_signed_disallowed():
    sm = ReportStateMachine()
    conn = StubConn()
    actor = uuid4()
    now = datetime.now(timezone.utc)
    conn.push_fetchrow(
        _row(
            status="signed",
            primary_author_id=actor,
            finalized_at=now - timedelta(minutes=10),
        ),
    )
    with pytest.raises(IllegalTransitionError):
        await sm.revert_to_draft(
            conn, report_id=uuid4(), actor_user_id=actor, now=now
        )


# ── Allowed-action table coverage (sync tests) ─────────────────────


def test_allowed_actions_match_spec_table():
    sm = ReportStateMachine()
    assert set(sm.allowed_actions(ReportStatus.DRAFT)) == {
        TransitionAction.FINALIZE, TransitionAction.CANCEL
    }
    assert set(sm.allowed_actions(ReportStatus.FINALIZED)) == {
        TransitionAction.REVERT_TO_DRAFT,
        TransitionAction.SIGN,
        TransitionAction.CANCEL,
    }
    assert set(sm.allowed_actions(ReportStatus.SIGNED)) == {
        TransitionAction.AMEND
    }
    assert sm.allowed_actions(ReportStatus.AMENDED) == []
    assert sm.allowed_actions(ReportStatus.CANCELLED) == []


def test_expected_to_lookup_raises_on_disallowed():
    sm = ReportStateMachine()
    with pytest.raises(IllegalTransitionError):
        sm.expected_to(ReportStatus.AMENDED, TransitionAction.FINALIZE)
    with pytest.raises(IllegalTransitionError):
        sm.expected_to(ReportStatus.CANCELLED, TransitionAction.AMEND)
