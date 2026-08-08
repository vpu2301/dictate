"""evidence-answer — Quick Search question engine (:8013)."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from observability import bootstrap, register_exception_handlers
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from evidence_answer.config import settings
from evidence_answer.deps import close_auth
from evidence_answer.main_deps import build_state, install_state, teardown_state
from evidence_answer.routers import answers, health, questions, suggestions

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="evidence-answer",
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
    await close_auth()
    logger.info("Service shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Evidence Answer",
        description=(
            "Quick Search: intake/triage, intent extraction, staged pipeline, "
            "SSE streaming, persistence and provenance"
        ),
        version="0.1.0",
        openapi_version="3.1.0",
        lifespan=_lifespan,
    )
    register_exception_handlers(app)
    # Unlike /retrieve, this service is browser-facing (the dictat evidence
    # module calls POST /answers directly in dev).
    origins = settings.cors_origins_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            # SSE clients read this to recover an answer id after a drop.
            expose_headers=["X-Answer-Id"],
            max_age=600,
        )
    app.include_router(health.router)
    app.include_router(answers.router)
    app.include_router(questions.router)
    app.include_router(suggestions.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
