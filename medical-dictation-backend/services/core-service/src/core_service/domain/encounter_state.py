"""Encounter (visit) lifecycle state machine.

Same shape as dictation-service's ``session/state.py``: an explicit
transition table plus a guard, so an illegal move is a 409 with a readable
reason rather than a silent UPDATE.

::

    scheduled ──start──> in_progress <──resume── paused
        │                    │  │  └──pause──────┘
        │                    │  └──complete──> completed  (terminal)
        └──cancel───────────┴─────cancel────> cancelled  (terminal)

``completed`` and ``cancelled`` are terminal: a visit that is over stays
over. Re-opening is deliberately not a transition — the clinician records a
new encounter instead, which keeps the audit trail honest about how many
times a patient was actually seen.
"""

from __future__ import annotations

from typing import Final, Literal

EncounterStatus = Literal["scheduled", "in_progress", "paused", "completed", "cancelled"]

SCHEDULED: Final = "scheduled"
IN_PROGRESS: Final = "in_progress"
PAUSED: Final = "paused"
COMPLETED: Final = "completed"
CANCELLED: Final = "cancelled"

#: Statuses that still hold a slot in the clinician's pipeline.
OPEN_STATUSES: Final[frozenset[str]] = frozenset({IN_PROGRESS, PAUSED})
#: Statuses a visit can never leave.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({COMPLETED, CANCELLED})

_ALLOWED: Final[dict[str, frozenset[str]]] = {
    SCHEDULED: frozenset({IN_PROGRESS, CANCELLED}),
    IN_PROGRESS: frozenset({PAUSED, COMPLETED, CANCELLED}),
    PAUSED: frozenset({IN_PROGRESS, COMPLETED, CANCELLED}),
    COMPLETED: frozenset(),
    CANCELLED: frozenset(),
}

#: Verb → target status. The router exposes one endpoint per verb rather
#: than a generic PATCH, so an audit event maps 1:1 onto a clinical action.
ACTION_TARGET: Final[dict[str, str]] = {
    "start": IN_PROGRESS,
    "pause": PAUSED,
    "resume": IN_PROGRESS,
    "complete": COMPLETED,
    "cancel": CANCELLED,
}

#: These messages are shown to a clinician mid-visit, so they are spelled
#: out rather than built by suffixing the verb (which yields "completeed").
_PAST_PARTICIPLE: Final[dict[str, str]] = {
    "start": "started",
    "pause": "paused",
    "resume": "resumed",
    "complete": "completed",
    "cancel": "cancelled",
}


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def is_open(status: str) -> bool:
    return status in OPEN_STATUSES


def can_transition(current: str, target: str) -> bool:
    return target in _ALLOWED.get(current, frozenset())


def transition_error(current: str, action: str) -> str | None:
    """Human-readable reason the action is refused, or None if it is legal.

    ``resume`` and ``start`` share a target status, so they are separated
    here: resuming something that was never paused is a different mistake
    from starting something already underway, and the clinician should be
    told which.
    """
    target = ACTION_TARGET[action]
    if current == target and action in {"start", "resume"}:
        return f"encounter is already {current}"
    if is_terminal(current):
        return (
            f"encounter is already {current} and cannot be "
            f"{_PAST_PARTICIPLE[action]}"
        )
    if action == "resume" and current != PAUSED:
        return f"cannot resume an encounter that is {current}"
    if action == "pause" and current != IN_PROGRESS:
        return f"cannot pause an encounter that is {current}"
    if not can_transition(current, target):
        return f"cannot {action} an encounter that is {current}"
    return None
