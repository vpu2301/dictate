"""signing-service entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from observability import bootstrap, register_exception_handlers

from .config import settings
from .deps import install_state
from .main_deps import build_state, teardown_state
from .middleware import (
    PublicVerifySecurityHeadersMiddleware,
    RequestIDMiddleware,
)
from .routers import callbacks, health, sessions, verify

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="signing-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )
    state = await build_state()
    app.state.svc = state
    install_state(state)
    logger.info(
        "signing-service.started",
        extra={"service": settings.service_name, "env": settings.environment},
    )
    try:
        yield
    finally:
        await teardown_state(state)
        logger.info("signing-service.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Signing Service",
        description="Sprint-09 KEP signing + public /verify.",
        version="0.9.0",
        openapi_version="3.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.add_middleware(PublicVerifySecurityHeadersMiddleware)
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(callbacks.router)
    app.include_router(verify.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
