"""dictation-service entry point.

Sprint 04 surface:
- ``/healthz`` / ``/readyz``
- ``/dictate/sessions/...`` HTTP companion endpoints
- ``/ws/dictate`` WebSocket streaming endpoint (medical-dictation.v1)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from observability import bootstrap, register_exception_handlers

from . import telemetry
from .config import settings
from .deps import install_state
from .main_deps import build_state, teardown_state
from .middleware import RequestIDMiddleware
from .routers import health, internal, sessions, ws
from .session.reaper import reaper_loop
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

    # Sprint 16 (MDX_WARM_IN_BACKGROUND): warm Whisper (+ diarizer) off
    # the startup path. faster-whisper's loader holds the GIL, so it runs
    # in a thread; /readyz gates on engine.is_loaded meanwhile.
    warm_task: asyncio.Task[None] | None = None
    if settings.warm_in_background:

        async def _warm_models() -> None:
            try:
                await asyncio.to_thread(state.engine.load)
                logger.info(
                    "warmup.whisper_ready",
                    extra={"warmup_seconds": state.engine.warmup_seconds},
                )
            except Exception:  # noqa: BLE001 — stay alive; readyz stays 503
                logger.exception("warmup.whisper_failed")
                return
            if settings.diar_warm_at_startup:
                try:
                    await state.diarization_engine.warm_up()
                except Exception:  # noqa: BLE001 — dictation-only worker is fine
                    logger.exception("warmup.diarizer_failed")

        warm_task = asyncio.create_task(_warm_models(), name="model-warmup")

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
                await asyncio.wait_for(hb_stop.wait(), timeout=settings.worker_heartbeat_interval_s)
                return
            except TimeoutError:
                continue

    hb_task = asyncio.create_task(_hb_loop())

    # Worker-state gauges (capacity weight, per-mode sessions, model
    # residency, device memory). Sampled on a timer because they describe a
    # standing condition, not an event — see telemetry.gauge_loop.
    gauge_stop = asyncio.Event()
    gauge_task = asyncio.create_task(telemetry.gauge_loop(state, gauge_stop))

    # Out-of-process backstop for sessions stranded by a dead worker — the
    # in-process abandon timer cannot survive the process that owns it.
    reaper_stop = asyncio.Event()
    reaper_task: asyncio.Task[None] | None = None
    if settings.session_reaper_enabled:
        reaper_task = asyncio.create_task(reaper_loop(state, reaper_stop))

    logger.info(
        "dictation-service.started",
        extra={
            "service": settings.service_name,
            "env": settings.environment,
            "worker_id": settings.worker_id,
            "model": state.engine.model_name,
            # Conversation capacity is a deployment-visible property of this
            # worker: a cold diarizer means dictation-only.
            "conversation_ready": state.diarization_engine.ready_for_conversation,
            "conversation_session_weight": settings.conversation_session_weight,
            "per_worker_max_sessions": settings.per_worker_max_sessions,
        },
    )
    try:
        yield
    finally:
        if warm_task is not None:
            warm_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await warm_task
        hb_stop.set()
        gauge_stop.set()
        reaper_stop.set()
        hb_task.cancel()
        gauge_task.cancel()
        if reaper_task is not None:
            reaper_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await hb_task
        with suppress(asyncio.CancelledError, Exception):
            await gauge_task
        if reaper_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await reaper_task
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
    app.include_router(internal.router)
    app.include_router(sessions.router)
    app.include_router(ws.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
