"""generation-service entry point."""

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
from .main_deps import build_state, teardown_state
from .middleware import RequestIDMiddleware
from .routers import completions, health

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="generation-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )
    state = await build_state()
    app.state.svc = state
    install_state(state)
    # Sprint 16 (MDX_PREWARM_ENABLED): force the model resident with a
    # 1-token completion before advertising readiness — a llama-server
    # that answers /health can still owe its first-token latency to lazy
    # weight residency/KV allocation. Retries until it lands; /readyz
    # reports `warmed: false` (503) meanwhile.
    state.warmed = not settings.prewarm_enabled
    warm_task: asyncio.Task[None] | None = None
    if (
        settings.prewarm_enabled
        and settings.layer_c_enabled
        and not settings.testing
    ):

        async def _prewarm() -> None:
            while True:
                try:
                    if state.inference is not None and await state.inference.ready():
                        await state.inference.complete(
                            prompt="Warmup.", max_tokens=1
                        )
                        state.warmed = True
                        logger.info("warmup.generation_ready")
                        return
                except Exception as exc:  # noqa: BLE001 — keep retrying
                    logger.warning(
                        "warmup.generation_retry", extra={"error": str(exc)}
                    )
                await asyncio.sleep(settings.prewarm_retry_seconds)

        warm_task = asyncio.create_task(_prewarm(), name="generation-prewarm")
    logger.info(
        "generation-service.started",
        extra={
            "service": settings.service_name,
            "env": settings.environment,
            "layer_c_enabled": settings.layer_c_enabled,
            "backend": settings.gen_backend,
            "model": settings.gen_model,
        },
    )
    try:
        yield
    finally:
        if warm_task is not None:
            warm_task.cancel()
        await teardown_state(state)
        logger.info("generation-service.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Generation Service",
        description="Sprint-15 Layer C inline generative completion (ADR-0036).",
        version="0.15.0",
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
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["WWW-Authenticate"],
        max_age=600,
    )
    app.include_router(health.router)
    app.include_router(completions.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
