"""Liveness + readiness probes (Kubernetes naming, per the template)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    state = getattr(request.app.state, "svc", None)
    if state is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "starting"}
    try:
        async with state.app_pool.acquire() as conn:
            await conn.execute("SELECT 1")
        await state.redis.ping()
    except Exception:  # noqa: BLE001 — any probe failure means not-ready
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unready"}
    return {"status": "ready"}
