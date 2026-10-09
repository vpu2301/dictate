"""autocomplete-service entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from observability import bootstrap, register_exception_handlers

from .config import settings
from .deps import install_state
from .jobs import partition_rotation, rollup
from .main_deps import build_state, teardown_state
from .middleware import RequestIDMiddleware
from .routers import (
    compliance,
    corpus,
    corpus_eval,
    corpus_eval_pipeline,
    health,
    phrases,
    suggest,
    telemetry,
)

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
    # Maintenance loops run in-process (first iteration fires immediately, so
    # a fresh deployment self-heals missing telemetry partitions). Both jobs
    # are idempotent; disable via MDX_BACKGROUND_JOBS when an external
    # scheduler owns them.
    jobs: list[asyncio.Task[None]] = []
    if settings.background_jobs_enabled and not settings.testing:
        interval = settings.background_jobs_interval_s
        jobs = [
            asyncio.create_task(
                partition_rotation.run_forever(interval_seconds=interval),
                name="partition-rotation",
            ),
            asyncio.create_task(
                rollup.run_forever(interval_seconds=interval), name="rollup"
            ),
        ]
    logger.info(
        "autocomplete-service.started",
        extra={"service": settings.service_name, "env": settings.environment},
    )
    try:
        yield
    finally:
        for task in jobs:
            task.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
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
    # CORS for the SPA. allow_credentials=True is required so the browser sends
    # the HttpOnly `mdx_rt` cookie on cross-origin XHR; that forbids a wildcard
    # origin, so origins are an explicit allow-list (mirror auth-service A3).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["WWW-Authenticate"],
        max_age=600,
    )
    app.include_router(health.router)
    app.include_router(suggest.router)
    app.include_router(phrases.router)
    app.include_router(telemetry.router)
    app.include_router(corpus.router)
    app.include_router(corpus_eval.router)
    app.include_router(corpus_eval_pipeline.router)
    app.include_router(compliance.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
