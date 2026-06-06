"""Template service entry point.

Use ``create_app()`` for tests and ``uvicorn template_service.main:app``
or ``uvicorn template_service.main:create_app --factory`` for production.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from observability import bootstrap, register_exception_handlers

from .config import settings
from .main_deps import close_auth
from .middleware import RequestIDMiddleware
from .routers import health, whoami

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="template-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )
    if settings.auth_bypass_dev:
        logger.warning(
            "AUTH_BYPASS_DEV=true — authentication is disabled. "
            "This must never be set in staging or production.",
        )
    logger.info(
        "Service starting",
        extra={"service": settings.service_name, "env": settings.environment},
    )
    yield
    await close_auth()
    logger.info("Service shutting down")


def create_app() -> FastAPI:
    """Application factory. Returns a fully-wired FastAPI instance."""
    app = FastAPI(
        title="Template Service",
        description="Baseline service template. Copy `services/_template/` to create a new service.",
        version="0.1.0",
        openapi_version="3.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(whoami.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
