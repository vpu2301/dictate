"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

router = APIRouter()


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    """Ready only when both dependencies answer.

    Redis is not optional here the way it is for a cache-backed service:
    it carries the event stream AND the cross-worker fan-out, so a
    worker that cannot reach it would accept sockets it can never push
    to.
    """
    state = getattr(request.app.state, "svc", None)
    if state is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "starting"}
    try:
        async with state.app_pool.acquire() as conn:
            await conn.execute("SELECT 1")
        await state.redis.ping()
    except Exception:  # noqa: BLE001
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unready"}
    return {"status": "ready"}
