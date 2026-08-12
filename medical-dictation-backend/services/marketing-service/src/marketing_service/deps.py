"""Router-facing accessor for the service state."""

from __future__ import annotations

from .main_deps import ServiceState

_STATE: ServiceState | None = None


def install_state(state: ServiceState) -> None:
    global _STATE
    _STATE = state


def get_state() -> ServiceState:
    if _STATE is None:  # pragma: no cover — lifespan always installs it
        raise RuntimeError("service state not installed")
    return _STATE
