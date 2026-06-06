"""Liveness + readiness probes.

Readiness checks DB / Redis / GPU / Whisper-loaded — sprint 04 §1.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

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
        pong = await state.redis.ping()
        if not pong:
            redis_ok = "fail: no pong"
    except Exception as exc:  # noqa: BLE001
        redis_ok = f"fail: {type(exc).__name__}"
    model_loaded = state.engine.is_loaded
    gpu_available = _gpu_available()
    ok = (
        db_ok == "ok"
        and redis_ok == "ok"
        and model_loaded
    )
    response.status_code = (
        status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return ReadyResponse(
        status="ready" if ok else "not_ready",
        db=db_ok,
        redis=redis_ok,
        model_loaded=model_loaded,
        gpu_available=gpu_available,
    )


def _gpu_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False
