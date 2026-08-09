"""Liveness + readiness probes.

Readiness includes the Layer C posture: a pod with the feature disabled
is READY (a switched-off feature is not an outage), but reports
``layer_c_enabled: false`` so dashboards can tell the two states apart.
With the feature on, an unreachable inference backend means unready —
serving guaranteed-204s while pretending health would hide a real
failure (the MDX_CONVERSATION_ENABLED /readyz precedent).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from ..config import settings

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, object]:
    state = getattr(request.app.state, "svc", None)
    if state is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "starting"}
    try:
        await state.redis.ping()
    except Exception:  # noqa: BLE001 — any probe failure means not-ready
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unready", "layer_c_enabled": settings.layer_c_enabled}
    if not settings.layer_c_enabled:
        return {"status": "ready", "layer_c_enabled": False}
    backend_ok = state.inference is not None and await state.inference.ready()
    if not backend_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "unready",
            "layer_c_enabled": True,
            "reason": "inference backend unreachable",
        }
    # Sprint 16 pre-warm: a reachable backend that hasn't produced its
    # first token yet is not ready — the LB must not send traffic that
    # would pay the residency cost inline.
    if not getattr(state, "warmed", True):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "unready",
            "layer_c_enabled": True,
            "reason": "model warming",
            "warmed": False,
        }
    return {
        "status": "ready",
        "layer_c_enabled": True,
        "model": settings.gen_model,
        "warmed": True,
    }
