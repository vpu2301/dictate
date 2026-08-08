"""evidence-retrieval — hybrid retrieval + connector registry (:8011)."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from observability import bootstrap, register_exception_handlers
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from evidence_retrieval.config import settings
from evidence_retrieval.main_deps import build_state, install_state, teardown_state
from evidence_retrieval.routers import health, retrieve

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="evidence-retrieval",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )
    state = None
    if not settings.testing:
        state = await build_state()
        install_state(state)
        app.state.svc = state
    logger.info("Service starting", extra={"service": settings.service_name})
    yield
    if state is not None:
        await teardown_state(state)
    logger.info("Service shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Evidence Retrieval",
        description="Hybrid dense+lexical retrieval, RRF fusion, reranking, connector registry",
        version="0.1.0",
        openapi_version="3.1.0",
        lifespan=_lifespan,
    )
    register_exception_handlers(app)
    # CORS is off unless an environment opts in (see config.cors_allowed_origins).
    # A browser only ever reaches this service through the dictat retrieval
    # playground, which is itself dev-machine-only; outside development an
    # allow-list here means someone pointed a browser at an internal hop.
    origins = settings.cors_origins_list
    if origins:
        if not settings.is_development:
            logger.warning(
                "CORS allow-list set outside development — /retrieve is an "
                "internal hop and should not be browser-reachable",
                extra={"origins": origins, "environment": settings.environment},
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )
    app.include_router(health.router)
    app.include_router(retrieve.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
