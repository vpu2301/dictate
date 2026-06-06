"""dictation-service entry point.

Sprint 04 surface:
- ``/healthz`` / ``/readyz``
- ``/dictate/sessions/...`` HTTP companion endpoints
- ``/ws/dictate`` WebSocket streaming endpoint (medical-dictation.v1)
"""

from __future__ import annotations

import asyncio
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
from .routers import health, sessions, ws
from .session.resume import heartbeat_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="dictation-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )

    state = await build_state()
    app.state.svc = state
    install_state(state)

    # Inference queue runs as a background task.
    await state.inference_queue.__aenter__()

    # Worker liveness heartbeat — used by resume to detect dead workers.
    hb_stop = asyncio.Event()

    async def _hb_loop() -> None:
        while not hb_stop.is_set():
            try:
                await heartbeat_worker(state.redis)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "worker.heartbeat_failed",
                    extra={"error": str(exc), "error_class": type(exc).__name__},
                )
            try:
                await asyncio.wait_for(
                    hb_stop.wait(), timeout=settings.worker_heartbeat_interval_s
                )
                return
            except asyncio.TimeoutError:
                continue

    hb_task = asyncio.create_task(_hb_loop())

    logger.info(
        "dictation-service.started",
        extra={
            "service": settings.service_name,
            "env": settings.environment,
            "worker_id": settings.worker_id,
            "model": state.engine.model_name,
        },
    )
    try:
        yield
    finally:
        hb_stop.set()
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass
        await state.inference_queue.__aexit__(None, None, None)
        await teardown_state(state)
        logger.info("dictation-service.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Dictation Service",
        description="Sprint-04 streaming ASR over WebSockets (medical-dictation.v1).",
        version="0.4.0",
        openapi_version="3.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(ws.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
