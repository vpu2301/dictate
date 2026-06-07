"""Liveness + readiness probes.

``/readyz`` returns 503 until the punctuation model has loaded
(sprint-05 spec E10). The fallback path is for runtime degradation,
not for permanent absence — k8s readiness gating during rollout means
traffic only lands on workers that have the model.
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
    punctuation_model_loaded: bool


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
    summary="Readiness probe — DB, Redis, punctuation model",
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

    model_loaded = state.punctuation_stage.is_loaded
    ok = (db_ok == "ok") and (redis_ok == "ok") and model_loaded
    response.status_code = status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(
        status="ready" if ok else "not_ready",
        db=db_ok,
        redis=redis_ok,
        punctuation_model_loaded=model_loaded,
    )
