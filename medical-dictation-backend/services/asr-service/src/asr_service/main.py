"""asr-service entry point.

Sprint 03 surface:
- ``/healthz`` / ``/readyz``
- ``/asr/jobs``   POST upload, GET list, GET id, DELETE id

Use ``create_app()`` for tests; production runs via
``uvicorn asr_service.main:app --host 0.0.0.0 --port 8000``.
"""

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
from .middleware import RequestIDMiddleware
from .routers import health, jobs

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="asr-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )

    state = await build_state()
    app.state.svc = state
    install_state(state)

    logger.info(
        "asr-service starting",
        extra={
            "service": settings.service_name,
            "env": settings.environment,
            "issuer": settings.auth_issuer,
            "audio_bucket": settings.s3_audio_bucket,
        },
    )
    try:
        yield
    finally:
        await teardown_state(state)
        logger.info("asr-service shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="ASR Service",
        description="Sprint-03 batch ASR orchestrator (upload, queue, fetch).",
        version="0.3.0",
        openapi_version="3.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(jobs.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
