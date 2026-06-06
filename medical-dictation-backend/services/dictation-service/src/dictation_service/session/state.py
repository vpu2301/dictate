"""Session state machine.

State graph (canonical, see docs/api/dictation-ws-v1.md § lifecycle):

    creating ──► active ──► finalized
                  │ ▲
                  ▼ │
                paused
                  │
                  ▼
              reconnecting ──► finalized | abandoned | failed
                  │
                  ▼
                 failed

Transitions are explicit. ``can_transition`` is the gate every code path
uses before mutating ``dictation_sessions.status`` so an invalid
transition fails fast instead of corrupting state.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class SessionState(StrEnum):
    CREATING = "creating"
    ACTIVE = "active"
    PAUSED = "paused"
    RECONNECTING = "reconnecting"
    FINALIZED = "finalized"
    ABANDONED = "abandoned"
    FAILED = "failed"


class StateTransitionError(Exception):
    pass


# from-state → allowed to-states
_ALLOWED: Final[dict[SessionState, frozenset[SessionState]]] = {
    SessionState.CREATING: frozenset({SessionState.ACTIVE, SessionState.FAILED}),
    SessionState.ACTIVE: frozenset({
        SessionState.PAUSED,
        SessionState.RECONNECTING,
        SessionState.FINALIZED,
        SessionState.FAILED,
    }),
    SessionState.PAUSED: frozenset({
        SessionState.ACTIVE,
        SessionState.RECONNECTING,
        SessionState.FINALIZED,
        SessionState.FAILED,
    }),
    SessionState.RECONNECTING: frozenset({
        SessionState.ACTIVE,
        SessionState.ABANDONED,
        SessionState.FAILED,
        SessionState.FINALIZED,  # force-finalize on stuck session
    }),
    # Terminal states — no outgoing transitions.
    SessionState.FINALIZED: frozenset(),
    SessionState.ABANDONED: frozenset(),
    SessionState.FAILED: frozenset(),
}


def can_transition(from_state: SessionState, to_state: SessionState) -> bool:
    return to_state in _ALLOWED.get(from_state, frozenset())


def assert_transition(from_state: SessionState, to_state: SessionState) -> None:
    """Raise :class:`StateTransitionError` if the transition is invalid.

    Used at every mutation site so an invalid transition surfaces at the
    earliest possible point and is loud in the logs.
    """
    if not can_transition(from_state, to_state):
        raise StateTransitionError(
            f"invalid transition {from_state.value!r} → {to_state.value!r}"
        )


def is_terminal(state: SessionState) -> bool:
    return state in {SessionState.FINALIZED, SessionState.ABANDONED, SessionState.FAILED}


def is_live(state: SessionState) -> bool:
    """A 'live' session can still receive audio or be resumed."""
    return state in {SessionState.ACTIVE, SessionState.PAUSED, SessionState.RECONNECTING}
