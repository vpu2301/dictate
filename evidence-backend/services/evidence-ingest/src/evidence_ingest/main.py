"""evidence-ingest — corpus ingestion service (:8010)."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from observability import bootstrap, register_exception_handlers
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from evidence_ingest.config import settings
from evidence_ingest.deps import close_auth
from evidence_ingest.main_deps import build_state, install_state, teardown_state
from evidence_ingest.routers import health, jobs, snapshots

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="evidence-ingest",
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
        title="Evidence Ingest",
        description="Corpus ingestion: parse → chunk → enrich → dedup → embed → index → snapshot",
        version="0.1.0",
        openapi_version="3.1.0",
        lifespan=_lifespan,
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(snapshots.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
