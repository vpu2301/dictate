"""Three distinct middleware chains for signing-service.

Each route is registered with exactly one chain; mismatches caught
by a CI lint test (``tests/test_route_chain_lint.py``).

Chains:
- **public_verify_chain**: `/verify/*` — no auth, IP-rate-limited,
  security headers, no Set-Cookie.
- **callback_chain**: `/signing/callbacks/*` — provider-signature
  validated; provider-specific dispatch.
- **internal_chain**: `/signing/sessions*`, `/signing/health` —
  service-account JWT required.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class PublicVerifySecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamps the canonical security headers on every ``/verify/*``
    response. Tested in tests/unit/test_public_verify_security.py.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        if not request.url.path.startswith("/verify"):
            return response
        # Default-deny security posture on the public surface.
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=63072000; includeSubDomains; preload",
        )
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; sandbox; base-uri 'none';",
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate")
        # Strip any accidental Set-Cookie.
        if "set-cookie" in {h.lower() for h in response.headers}:
            del response.headers["set-cookie"]
        return response


# Route classifier used by the lint test.
def chain_for_path(path: str) -> str:
    if path.startswith("/verify"):
        return "public_verify_chain"
    if path.startswith("/signing/callbacks/"):
        return "callback_chain"
    if path.startswith("/signing/"):
        return "internal_chain"
    return "internal_chain"
