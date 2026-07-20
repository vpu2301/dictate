"""The `/ws/notifications` route.

Thin on purpose: authorize, then hand off. All the policy lives in
ws/upgrade.py and all the behaviour in ws/handler.py, so this file
stays a wiring detail.
"""

from __future__ import annotations

from fastapi import APIRouter
from starlette.websockets import WebSocket

from ..deps import get_state
from ..ws.handler import run_session
from ..ws.upgrade import UpgradeRejected, authorize_upgrade, ws_code_for_http

router = APIRouter()


@router.websocket("/ws/notifications")
async def notifications_ws(websocket: WebSocket) -> None:
    state = get_state()
    try:
        upgrade = await authorize_upgrade(websocket, jwks_cache=state.jwks_cache)
    except UpgradeRejected as rejection:
        # Closing with a mapped 4xxx code gives the client the reason.
        # We must accept() first to be able to send a close code at all
        # once the handshake has begun.
        await websocket.close(code=ws_code_for_http(rejection.status_code))
        return

    await run_session(websocket, upgrade=upgrade, state=state)
