"""RFC 9457 Problem Details — single source of truth for HTTP error envelopes.

Every service uses ``register_exception_handlers(app)`` to install global
handlers for ``HTTPException``, ``RequestValidationError``, and unhandled
exceptions. The ``instance`` field carries a fresh ``urn:uuid:`` token that
is also logged so support can correlate a user-visible error to the trace.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

logger = logging.getLogger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"

_DEFAULT_TYPE = "about:blank"

_HTTP_STATUS_TITLES: dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    415: "Unsupported Media Type",
    422: "Unprocessable Content",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class ProblemDetails(BaseModel):
    """RFC 9457 Problem Details document."""

    type: str = Field(default=_DEFAULT_TYPE)
    title: str
    status: int
    detail: str | None = None
    instance: str
    # Extension members are allowed; FastAPI emits `model_extra` if set.

    model_config = {"extra": "allow"}


def _new_instance() -> str:
    return f"urn:uuid:{uuid4()}"


def _problem(
    *, status: int, detail: str | None, type_uri: str | None = None, **extras: Any
) -> ProblemDetails:
    return ProblemDetails(
        type=type_uri or _DEFAULT_TYPE,
        title=_HTTP_STATUS_TITLES.get(status, "Error"),
        status=status,
        detail=detail,
        instance=_new_instance(),
        **extras,
    )


def _json_response(p: ProblemDetails, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=p.status,
        content=p.model_dump(mode="json", exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


async def http_exception_handler(
    request: Request, exc: HTTPException | StarletteHTTPException
) -> JSONResponse:
    p = _problem(status=exc.status_code, detail=str(exc.detail))
    logger.info(
        "http_exception",
        extra={
            "status": exc.status_code,
            "instance": p.instance,
            "path": str(request.url.path),
            "method": request.method,
        },
    )
    # Preserve headers the raiser set (notably WWW-Authenticate on 401).
    extra_headers = getattr(exc, "headers", None)
    return _json_response(p, headers=extra_headers)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    p = _problem(
        status=422,
        detail="Request validation failed.",
        type_uri="https://datatracker.ietf.org/doc/html/rfc9457#section-3",
        errors=exc.errors(),
    )
    logger.info(
        "validation_error",
        extra={
            "instance": p.instance,
            "path": str(request.url.path),
            "method": request.method,
        },
    )
    return _json_response(p)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    p = _problem(status=500, detail="An unexpected error occurred.")
    logger.exception(
        "unhandled_exception",
        extra={"instance": p.instance, "path": str(request.url.path), "method": request.method},
    )
    return _json_response(p)


def register_exception_handlers(app: FastAPI) -> None:
    """Install RFC 9457 handlers for HTTPException, validation errors, and unhandled."""
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
