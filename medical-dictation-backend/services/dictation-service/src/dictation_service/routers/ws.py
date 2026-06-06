"""``/ws/dictate`` route registration.

Starlette dispatches WebSocket handlers via async function endpoints
registered with ``@router.websocket(...)``. The upgrade-time auth +
subprotocol negotiation happens before ``accept()`` so a rejected
upgrade returns plain HTTP.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket

from ..deps import get_state
from ..ws.handler import run_session
from ..ws.upgrade import UpgradeRejected, authorize_upgrade

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/dictate")
async def dictate(websocket: WebSocket) -> None:
    state = get_state()
    try:
        upgrade = await authorize_upgrade(
            websocket,
            jwks_cache=state.jwks_cache,
            redis=state.redis,
            audit_writer=state.audit_writer,
        )
    except UpgradeRejected as rej:
        # Starlette won't send an HTTP error after `websocket.accept()`,
        # so we explicitly close without accepting. For the 101 handshake
        # to fail with a meaningful HTTP status, Starlette translates
        # ``websocket.close(code=...)`` BEFORE accept into an HTTP error.
        await websocket.close(code=_ws_code_for_http(rej.status_code))
        return

    await run_session(websocket, upgrade=upgrade, state=state)


def _ws_code_for_http(http_code: int) -> int:
    """Map HTTP rejection codes to WS close codes (RFC 6455 reserved codes)."""
    if http_code == 401:
        return 4401  # custom range — frontend interprets
    if http_code == 403:
        return 4403
    if http_code == 400:
        return 4400
    if http_code == 429:
        return 4429
    return 1008
