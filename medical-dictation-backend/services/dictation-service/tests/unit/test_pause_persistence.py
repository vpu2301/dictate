"""Pause/resume must reach the database.

As built, ``Pause``/``Resume`` mutated ``ctx.state`` and nothing else. The
``'paused'`` value in the 0010 CHECK constraint was therefore dead in the
DB: ``GET /dictate/sessions`` reported a paused session as active, and so
did ``count_active_for_tenant``, which gates the per-tenant cap. Anything
outside the owning worker process — the reaper, the encounter's
"is a recording still live?" check, another tab — reads the row, so the row
has to be true.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import UUID

import pytest

from dictation_service.session.state import SessionState, StateTransitionError
from dictation_service.ws import handler

TENANT_ID = UUID("00000000-0000-0000-0000-0000000000aa")
SESSION_ID = UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, list[tuple]]:
    from dictation_service.domain import repository

    writes: list[tuple] = []

    async def _update_status(conn, *, session_id, new_status, **kw):  # noqa: ANN001
        writes.append((session_id, str(new_status)))

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(repository, "update_status", _update_status)
    monkeypatch.setattr(handler, "tenant_connection", _fake_tenant_conn)
    return SimpleNamespace(app_pool=object()), writes


def _ctx(state: SessionState) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        state=state,
        paused_at=None,
    )


async def test_pause_is_persisted(wiring: tuple[SimpleNamespace, list[tuple]]) -> None:
    state, writes = wiring
    ctx = _ctx(SessionState.ACTIVE)
    await handler.apply_pause(ctx, state)
    assert ctx.state is SessionState.PAUSED
    assert ctx.paused_at is not None
    assert writes == [(SESSION_ID, "paused")]


async def test_resume_is_persisted(wiring: tuple[SimpleNamespace, list[tuple]]) -> None:
    state, writes = wiring
    ctx = _ctx(SessionState.PAUSED)
    ctx.paused_at = 123.0
    await handler.apply_resume(ctx, state)
    assert ctx.state is SessionState.ACTIVE
    assert ctx.paused_at is None
    assert writes == [(SESSION_ID, "active")]


async def test_round_trip_leaves_the_row_active(
    wiring: tuple[SimpleNamespace, list[tuple]],
) -> None:
    state, writes = wiring
    ctx = _ctx(SessionState.ACTIVE)
    await handler.apply_pause(ctx, state)
    await handler.apply_resume(ctx, state)
    assert writes == [(SESSION_ID, "paused"), (SESSION_ID, "active")]


@pytest.mark.parametrize(
    "bad", [SessionState.FINALIZED, SessionState.ABANDONED, SessionState.FAILED]
)
async def test_pausing_a_terminal_session_is_refused_by_the_guard(
    wiring: tuple[SimpleNamespace, list[tuple]], bad: SessionState
) -> None:
    """These paths used to bypass ``assert_transition`` entirely."""
    state, writes = wiring
    with pytest.raises(StateTransitionError):
        await handler.apply_pause(_ctx(bad), state)
    assert writes == []
