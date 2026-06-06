"""Session-lifecycle primitives.

State machine is exported eagerly (pure stdlib). Manager / resume /
heartbeat / finalize are lazy because they pull in numpy or redis.
"""

from .state import SessionState, StateTransitionError, can_transition

__all__ = [
    "SessionState",
    "StateTransitionError",
    "can_transition",
]
