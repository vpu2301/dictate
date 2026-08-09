"""Loopback-only internal surface — the scale-in drain hook (sprint 16).

Kubernetes calls this from the pod's OWN preStop hook:

    preStop: curl -sf -XPOST localhost:8000/internal/drain
             then poll GET /internal/drain until active_sessions == 0
             (cap: terminationGracePeriodSeconds = 30 min)

Contract:

- ``POST /internal/drain``  — flip the one-way draining flag: the worker
  admits no new sessions (clients get the gpu_full reconnect semantics
  and land on another pod), live sessions run to completion, and
  ``/readyz`` goes 503 so the Service stops routing new connections.
- ``GET  /internal/drain``  — drain progress for the preStop poll loop.

Never exposed beyond the pod: the edge allowlist doesn't carry it, the
NetworkPolicy doesn't open it, and — defence in depth — the handler
refuses any non-loopback client address outright.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from ..deps import get_state

router = APIRouter(prefix="/internal", tags=["internal"])

_LOOPBACKS = {"127.0.0.1", "::1", "localhost"}


class DrainStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draining: bool
    active_sessions: int
    total_weight: int


def _loopback_only(request: Request) -> None:
    client = request.client.host if request.client else ""
    if client not in _LOOPBACKS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="internal surface is loopback-only",
        )


@router.post("/drain", response_model=DrainStatus, summary="Begin scale-in drain")
async def begin_drain(request: Request) -> DrainStatus:
    _loopback_only(request)
    state = get_state()
    state.session_manager.begin_drain()
    return _status(state)


@router.get("/drain", response_model=DrainStatus, summary="Drain progress")
async def drain_status(request: Request) -> DrainStatus:
    _loopback_only(request)
    return _status(get_state())


def _status(state: object) -> DrainStatus:
    manager = state.session_manager  # type: ignore[attr-defined]
    return DrainStatus(
        draining=manager.draining,
        active_sessions=manager.active_count,
        total_weight=manager.total_weight,
    )
