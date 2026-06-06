"""Kubernetes-style liveness/readiness."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe — pools reachable?")
async def readyz(request: Request) -> dict[str, str]:
    state = getattr(request.app.state, "svc", None)
    if state is None:
        return {"status": "starting"}
    # Cheap ping: one round trip on the app pool.
    async with state.app_pool.acquire() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ready"}
