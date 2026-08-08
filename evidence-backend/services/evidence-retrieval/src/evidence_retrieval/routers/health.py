from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from evidence_retrieval.main_deps import get_state

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "evidence-retrieval"}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    try:
        state = get_state()
    except RuntimeError:
        return JSONResponse(status_code=503, content={"status": "starting"})
    # Dense engine (pg) is mandatory; lexical + rerank degrade (spec §11).
    try:
        await state.app_pool.fetchval("SELECT 1")
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "unready", "reason": "database"})
    warnings: list[str] = []
    if not await state.lexical.ping():
        warnings.append("opensearch_unreachable")
    if not await state.gateway.ready():
        warnings.append("gateway_unready")
    return JSONResponse({"status": "ready", "warnings": warnings})
