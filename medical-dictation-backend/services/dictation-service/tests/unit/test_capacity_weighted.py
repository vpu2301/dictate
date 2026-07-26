"""Sprint-14 mode-aware (weighted) worker capacity.

A conversation session runs two resident models, so it costs
``conversation_session_weight`` slots (ADR-0034 §capacity). The cap
compares WEIGHT, not headcount: with max_sessions=4 that is 4 dictation
OR 2 conversation OR a 2+1+1 mix. Pure — the manager is in-memory only.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from dictation_service.session.manager import (
    CapacityError,
    SessionContext,
    SessionManager,
)


def _ctx(*, weight: int = 1, session_id: UUID | None = None) -> SessionContext:
    return SessionContext(
        session_id=session_id or uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        language="uk",
        prompt_id=uuid4(),
        prompt_text="",
        target_kind="generic",
        encounter_id=None,
        template_id=None,
        mode="conversation" if weight > 1 else "dictation",
        capacity_weight=weight,
    )


async def test_four_dictation_sessions_fill_the_worker() -> None:
    mgr = SessionManager(max_sessions=4)
    for _ in range(4):
        assert mgr.fits(1)
        await mgr.register(_ctx(weight=1))
    assert mgr.total_weight == 4
    assert mgr.total_count == 4
    assert not mgr.fits(1)
    with pytest.raises(CapacityError):
        await mgr.register(_ctx(weight=1))


async def test_two_conversation_sessions_fill_the_worker() -> None:
    mgr = SessionManager(max_sessions=4)
    await mgr.register(_ctx(weight=2))
    assert mgr.total_weight == 2
    assert mgr.fits(2)
    await mgr.register(_ctx(weight=2))
    assert mgr.total_weight == 4
    assert mgr.total_count == 2  # two SESSIONS, four slots
    assert not mgr.fits(2)
    assert not mgr.fits(1)
    with pytest.raises(CapacityError):
        await mgr.register(_ctx(weight=2))


async def test_mixed_conversation_plus_dictation_fits_exactly() -> None:
    mgr = SessionManager(max_sessions=4)
    await mgr.register(_ctx(weight=2))
    assert mgr.total_weight == 2
    assert mgr.fits(2) and mgr.fits(1)

    await mgr.register(_ctx(weight=1))
    assert mgr.total_weight == 3
    assert not mgr.fits(2)
    assert mgr.fits(1)

    await mgr.register(_ctx(weight=1))
    assert mgr.total_weight == 4
    assert mgr.total_count == 3
    assert not mgr.fits(1)
    assert not mgr.fits(2)

    with pytest.raises(CapacityError):
        await mgr.register(_ctx(weight=1))
    with pytest.raises(CapacityError):
        await mgr.register(_ctx(weight=2))


async def test_unregister_frees_the_full_weight() -> None:
    mgr = SessionManager(max_sessions=4)
    sid = uuid4()
    await mgr.register(_ctx(weight=2, session_id=sid))
    assert mgr.total_weight == 2

    freed = await mgr.unregister(sid)
    assert freed is not None
    assert mgr.total_weight == 0
    assert mgr.total_count == 0

    # The whole worker is available again — not just one slot.
    for _ in range(4):
        await mgr.register(_ctx(weight=1))
    assert mgr.total_weight == 4


async def test_capacity_error_message_reports_weights() -> None:
    mgr = SessionManager(max_sessions=4)
    await mgr.register(_ctx(weight=2))
    await mgr.register(_ctx(weight=2))
    with pytest.raises(CapacityError) as exc:
        await mgr.register(_ctx(weight=2))
    assert "weight 4" in str(exc.value)
