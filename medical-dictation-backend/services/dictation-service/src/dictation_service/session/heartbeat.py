"""Application-level heartbeat + token-expiry watchdog.

Both run as background tasks attached to each session. They emit server
messages (heartbeat, token_expiring) and tear the session down when the
client goes silent for ``ws_idle_timeout_s`` or when the JWT expires
without a refresh.

WS protocol-level ping/pong is intentionally NOT relied upon: corporate
proxies buffer pong frames in ways that mask real TCP failures. The
app-level heartbeat sees the latency the user sees.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..config import settings
from ..protocol import Heartbeat, TokenExpiring, encode_server
from .manager import SessionContext

logger = logging.getLogger(__name__)


async def heartbeat_loop(ctx: SessionContext) -> None:
    """Emit a server heartbeat every ``ws_heartbeat_interval_s``.

    Exits when the WS is gone (reconnect path takes over).
    """
    interval = settings.ws_heartbeat_interval_s
    while ctx.ws is not None:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        if ctx.ws is None:
            return
        msg = Heartbeat(server_time_ms=int(time.time() * 1000))
        try:
            await ctx.ws.send_text(encode_server(msg))
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "heartbeat.send_failed",
                extra={
                    "session_id": str(ctx.session_id),
                    "error": str(exc),
                    "error_class": type(exc).__name__,
                },
            )
            return


async def idle_watchdog(
    ctx: SessionContext,
    *,
    on_idle: object,
) -> None:
    """Close the WS if no client traffic for ``ws_idle_timeout_s``.

    ``on_idle`` is an async callable invoked when the watchdog fires;
    typically it transitions the session to ``reconnecting``.
    """
    timeout = settings.ws_idle_timeout_s
    while ctx.ws is not None:
        elapsed = time.monotonic() - ctx.last_active_at
        if elapsed > timeout:
            logger.info(
                "session.idle_timeout",
                extra={
                    "session_id": str(ctx.session_id),
                    "elapsed_s": round(elapsed, 2),
                    "timeout_s": timeout,
                },
            )
            try:
                await on_idle(ctx)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "idle_watchdog.on_idle_failed",
                    extra={"session_id": str(ctx.session_id), "error": str(exc)},
                )
            return
        try:
            await asyncio.sleep(max(1.0, timeout - elapsed))
        except asyncio.CancelledError:
            return


async def token_expiry_watchdog(ctx: SessionContext) -> None:
    """Emit `token_expiring` at T-60s, terminate session on expiry.

    The session loop catches the `refresh_token` message and replaces
    ``ctx.token_exp_ts``; the watchdog re-reads it on each pass.
    """
    warn_before = settings.session_token_expiry_warn_seconds
    while ctx.ws is not None and ctx.token_exp_ts is not None:
        now = time.time()
        remaining = ctx.token_exp_ts - now
        if remaining <= 0:
            # Token already expired — caller transitions to failed.
            return
        if remaining <= warn_before:
            try:
                await ctx.ws.send_text(encode_server(TokenExpiring(expires_in_s=int(remaining))))
            except Exception:
                return
            # Re-emit cadence: every 15 s while in the warn window so a
            # client that missed the first notice gets a second chance.
            sleep_for = 15.0
        else:
            sleep_for = remaining - warn_before
        try:
            await asyncio.sleep(max(1.0, sleep_for))
        except asyncio.CancelledError:
            return
