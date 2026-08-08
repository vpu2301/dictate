from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from evidence_ingest.main_deps import get_state

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "evidence-ingest"}


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
    # Embed role / OpenSearch down = degraded, not unready: jobs queue and
    # resume (spec §11), the ops API stays usable.
    warnings: list[str] = []
    if not await state.lexical.ping():
        warnings.append("opensearch_unreachable")
    if not await state.gateway.ready():
        warnings.append("embed_role_unready")
    return JSONResponse({"status": "ready", "warnings": warnings})
