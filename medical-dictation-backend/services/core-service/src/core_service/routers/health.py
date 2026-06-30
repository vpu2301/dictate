"""Liveness + readiness probes."""

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


@router.get(
    "/healthz",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Liveness probe",
)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/readyz", summary="Readiness probe — DB")
async def readyz(response: Response) -> ReadyResponse:
    state = get_state()
    db_ok = "ok"
    try:
        async with state.app_pool.acquire() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        db_ok = f"fail: {type(exc).__name__}"
    ok = db_ok == "ok"
    response.status_code = (
        status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return ReadyResponse(status="ready" if ok else "not_ready", db=db_ok)
