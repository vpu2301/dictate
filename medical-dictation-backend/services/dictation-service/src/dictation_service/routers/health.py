"""Liveness + readiness probes.

Readiness checks DB / Redis / GPU / Whisper-loaded — sprint 04 §1 — plus,
since sprint 14, whether this worker can actually take a CONVERSATION
session: that needs a second model (ECAPA + Silero) resident and warm.

The split matters for the fleet. A worker whose diarizer failed to load is
still a perfectly good dictation worker, so ``/readyz`` stays 200 and the
worker keeps serving — but ``conversation_ready`` goes false and the
scheduler/LB must not send conversation traffic to it. A worker that
advertised conversation capacity with a cold diarizer would pay weight
loading inside its first window and blow the latency budget.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import cast

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from ..config import settings
from ..deps import get_state

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: str
    db: str
    redis: str
    model_loaded: bool
    gpu_available: bool
    # ── sprint 14: conversation capacity ─────────────────────────────
    conversation_enabled: bool
    diarizer_loaded: bool
    conversation_ready: bool
    diarizer_error: str | None = None
    # Live capacity, so an operator (and sprint-16's HPA) can see WHY a
    # worker is refusing sessions without reading logs.
    capacity_used: int
    capacity_max: int
    conversation_session_weight: int
    conversation_slots_free: int


@router.get(
    "/healthz",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Liveness probe",
)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/readyz",
    summary="Readiness probe — DB, Redis, Whisper, GPU",
)
async def readyz(response: Response) -> ReadyResponse:
    state = get_state()
    db_ok = "ok"
    redis_ok = "ok"
    try:
        async with state.app_pool.acquire() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        db_ok = f"fail: {type(exc).__name__}"
    try:
        pong = await cast("Awaitable[bool]", state.redis.ping())
        if not pong:
            redis_ok = "fail: no pong"
    except Exception as exc:  # noqa: BLE001
        redis_ok = f"fail: {type(exc).__name__}"
    model_loaded = state.engine.is_loaded
    gpu_available = _gpu_available()

    diar = state.diarization_engine
    conversation_ready = diar.ready_for_conversation
    weight = settings.conversation_session_weight
    used = state.session_manager.total_weight
    free = max(0, settings.per_worker_max_sessions - used)

    # Whisper is the hard requirement: without it this worker serves
    # nothing. The diarizer is NOT — a dictation-only worker is healthy.
    # Draining (sprint-16 scale-in) also flips readiness: the Service
    # must stop routing NEW connections while live sessions finish.
    ok = (
        db_ok == "ok"
        and redis_ok == "ok"
        and model_loaded
        and not state.session_manager.draining
    )
    response.status_code = status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(
        status="ready" if ok else "not_ready",
        db=db_ok,
        redis=redis_ok,
        model_loaded=model_loaded,
        gpu_available=gpu_available,
        conversation_enabled=diar.enabled,
        diarizer_loaded=diar.loaded,
        conversation_ready=conversation_ready,
        diarizer_error=diar.last_error,
        capacity_used=used,
        capacity_max=settings.per_worker_max_sessions,
        conversation_session_weight=weight,
        # Zero unless the diarizer is warm — the whole point of the gate.
        conversation_slots_free=(free // weight) if (conversation_ready and weight > 0) else 0,
    )


def _gpu_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False
