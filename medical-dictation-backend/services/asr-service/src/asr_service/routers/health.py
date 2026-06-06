"""Liveness + readiness for asr-service.

``/readyz`` actively probes DB, Redis, and MinIO to support k8s readiness
gating. Sprint 03 introduces the first multi-dependency readiness path —
keep it cheap (each probe ≤ 250 ms) so the cluster doesn't churn replicas.
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
    s3: str


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
    status_code=status.HTTP_200_OK,
    summary="Readiness probe — verifies DB, Redis, MinIO reachable",
)
async def readyz(response: Response) -> ReadyResponse:
    state = get_state()
    db_ok = "ok"
    redis_ok = "ok"
    s3_ok = "ok"

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

    try:
        await state.s3.head_bucket(state.audio_store.bucket)
    except Exception as exc:  # noqa: BLE001
        s3_ok = f"fail: {type(exc).__name__}"

    if "ok" not in {db_ok, redis_ok, s3_ok} or any(
        v != "ok" for v in (db_ok, redis_ok, s3_ok)
    ):
        # Any non-ok flips the response code.
        if db_ok != "ok" or redis_ok != "ok" or s3_ok != "ok":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return ReadyResponse(status="not_ready", db=db_ok, redis=redis_ok, s3=s3_ok)
    return ReadyResponse(status="ready", db=db_ok, redis=redis_ok, s3=s3_ok)
