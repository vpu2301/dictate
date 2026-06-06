"""Exhaustive state-machine coverage."""

from __future__ import annotations

import pytest

from dictation_service.session.state import (
    SessionState,
    StateTransitionError,
    assert_transition,
    can_transition,
    is_live,
    is_terminal,
)


def test_legal_transitions_from_active() -> None:
    assert can_transition(SessionState.ACTIVE, SessionState.PAUSED)
    assert can_transition(SessionState.ACTIVE, SessionState.RECONNECTING)
    assert can_transition(SessionState.ACTIVE, SessionState.FINALIZED)
    assert can_transition(SessionState.ACTIVE, SessionState.FAILED)


def test_illegal_active_to_creating() -> None:
    assert not can_transition(SessionState.ACTIVE, SessionState.CREATING)
    with pytest.raises(StateTransitionError):
        assert_transition(SessionState.ACTIVE, SessionState.CREATING)


def test_terminal_states_have_no_transitions() -> None:
    for terminal in (SessionState.FINALIZED, SessionState.ABANDONED, SessionState.FAILED):
        for target in SessionState:
            assert not can_transition(terminal, target), (
                f"{terminal} → {target} should be illegal"
            )


def test_reconnecting_can_finalize_force() -> None:
    """sprint-04 day-6: POST /dictate/sessions/{id}/finalize while reconnecting."""
    assert can_transition(SessionState.RECONNECTING, SessionState.FINALIZED)


def test_is_terminal() -> None:
    assert is_terminal(SessionState.FINALIZED)
    assert is_terminal(SessionState.ABANDONED)
    assert is_terminal(SessionState.FAILED)
    assert not is_terminal(SessionState.ACTIVE)


def test_is_live() -> None:
    assert is_live(SessionState.ACTIVE)
    assert is_live(SessionState.PAUSED)
    assert is_live(SessionState.RECONNECTING)
    assert not is_live(SessionState.FINALIZED)


def test_pause_resume_cycle() -> None:
    assert can_transition(SessionState.ACTIVE, SessionState.PAUSED)
    assert can_transition(SessionState.PAUSED, SessionState.ACTIVE)


def test_no_resume_from_finalized() -> None:
    assert not can_transition(SessionState.FINALIZED, SessionState.ACTIVE)
