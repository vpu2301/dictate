"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

router = APIRouter()


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    """Ready when the database answers.

    The mail provider is deliberately NOT probed. Mail goes through an
    outbox, so a relay that is down means queued rows and a delayed
    send, not a dropped request — failing readiness would take the demo
    form offline to protect against a delay it already tolerates.
    """
    state = getattr(request.app.state, "svc", None)
    if state is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "starting"}
    try:
        async with state.app_pool.acquire() as conn:
            await conn.execute("SELECT 1")
    except Exception:  # noqa: BLE001
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unready"}
    return {"status": "ready"}
