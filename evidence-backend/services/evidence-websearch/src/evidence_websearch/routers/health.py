from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from evidence_websearch.main_deps import get_state

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "evidence-websearch"}


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
    # SearXNG or the embed role being down is DEGRADED, not unready: the
    # answer pipeline is designed to fall back to corpus-only (spec §11), and
    # taking this service out of rotation would turn a soft degrade into a
    # hard one for no benefit.
    warnings: list[str] = []
    if not await state.metasearch.ping():
        warnings.append("metasearch_unreachable")
    if not await state.gateway.ready():
        warnings.append("embed_role_unready")
    if not state.settings.egress_proxy_url and not state.settings.allow_direct_egress:
        warnings.append("egress_proxy_unconfigured")
    return JSONResponse({"status": "ready", "warnings": warnings})
