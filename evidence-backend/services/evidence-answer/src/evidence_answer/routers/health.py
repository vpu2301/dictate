from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from evidence_answer.main_deps import get_state

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "evidence-answer"}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    try:
        state = get_state()
    except RuntimeError:
        return JSONResponse(status_code=503, content={"status": "starting"})
    try:
        await state.app_pool.fetchval("SELECT 1")
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "unready", "reason": "database"})
    try:
        await state.redis.ping()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "unready", "reason": "redis"})
    # Unlike the web connector, these two are NOT degradable: without a
    # generator there is no answer, and without retrieval there is no evidence
    # to answer from. Reporting ready would mean serving `insufficient_basis`
    # to every caller while looking healthy.
    if not await state.gateway.ready():
        return JSONResponse(
            status_code=503, content={"status": "unready", "reason": "model_gateway"}
        )
    if not await state.retrieval.ready():
        return JSONResponse(status_code=503, content={"status": "unready", "reason": "retrieval"})
    return JSONResponse({"status": "ready"})
