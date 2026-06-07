"""Sprint-07: HTTP middleware that enforces the demo rate-limit on
session-creation endpoints.

Only loads when ``MDX_DEMO_MODE=true``. Production deployments have
this middleware absent from the FastAPI app stack.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from demo.audit_kinds import DEMO_AUDIT_KINDS
from demo.rate_limit import DemoRateLimiter, RateLimitConfig
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class DemoRateLimitMiddleware(BaseHTTPMiddleware):
    """Enforce per-IP/per-user demo caps on session-bearing endpoints.

    Records a `demo.rate_limit_hit` audit row on every 429 emitted.
    """

    def __init__(self, app, redis: Redis, audit, config: RateLimitConfig | None = None):
        super().__init__(app)
        self._limiter = DemoRateLimiter(redis, config)
        self._audit = audit
        self._guarded = {
            ("POST", "/v1/sessions"),
            ("POST", "/v1/sessions/start"),
        }

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if os.getenv("MDX_DEMO_MODE", "false").lower() != "true":
            return await call_next(request)
        if (request.method, request.url.path) not in self._guarded:
            return await call_next(request)

        ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0]
            or (request.client.host if request.client else "0.0.0.0")
        ).strip()
        user_id = getattr(request.state, "user_id", "anonymous")

        breach = await self._limiter.begin_session(ip=ip, user_id=user_id)
        if breach is not None:
            assert "demo.rate_limit_hit" in DEMO_AUDIT_KINDS
            try:
                await self._audit.append(
                    kind="demo.rate_limit_hit",
                    actor_user_id=user_id,
                    payload={
                        "ip": ip,
                        "axis": breach.kind,
                        "detail": breach.detail,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("audit append failed for rate-limit hit: %s", exc)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "axis": breach.kind,
                    "detail": breach.detail,
                },
                headers={"Retry-After": str(breach.retry_after_seconds)},
            )

        request.state.demo_ip = ip
        return await call_next(request)
