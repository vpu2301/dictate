"""autocomplete-service entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from observability import bootstrap, register_exception_handlers

from .config import settings
from .deps import install_state
from .main_deps import build_state, teardown_state
from .middleware import RequestIDMiddleware
from .routers import health, phrases, suggest, telemetry

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="autocomplete-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )
    state = await build_state()
    app.state.svc = state
    install_state(state)
    logger.info(
        "autocomplete-service.started",
        extra={"service": settings.service_name, "env": settings.environment},
    )
    try:
        yield
    finally:
        await teardown_state(state)
        logger.info("autocomplete-service.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Autocomplete Service",
        description="Sprint-10 autocomplete + snippet expansion.",
        version="0.10.0",
        openapi_version="3.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(suggest.router)
    app.include_router(phrases.router)
    app.include_router(telemetry.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
