"""core-service entry point (sprint-11 patients & per-patient record)."""

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
from .routers import (
    anamnesis,
    consents,
    encounters,
    health,
    notes,
    patient_documents,
    patients,
    privacy,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="core-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )
    state = await build_state()
    app.state.svc = state
    install_state(state)
    # Sprint 16: erasure backup-horizon notifier hosted in-process
    # (ADR-0041). Off by default in dev; also runnable as a CLI/cron
    # (python -m core_service.jobs.backup_horizon).
    jobs: list[asyncio.Task[None]] = []
    if settings.background_jobs_enabled and not settings.testing:
        from observability import run_periodic

        from .jobs import backup_horizon

        async def _backup_horizon_iteration() -> dict[str, int]:
            return await backup_horizon.run_once(
                app_pool=state.app_pool, audit_writer=state.audit_writer
            )

        jobs.append(
            asyncio.create_task(
                run_periodic(
                    job_name="erasure_backup_horizon",
                    interval_seconds=settings.background_jobs_interval_s,
                    fn=_backup_horizon_iteration,
                ),
                name="erasure-backup-horizon",
            )
        )
    logger.info(
        "core-service.started",
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
        logger.info("core-service.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Core Service",
        description="Sprint-11 clinical/EHR core: patients, encounters, notes, "
        "consents, anamnesis, privacy.",
        version="0.1.0",
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

    # Router order: more-specific collection routes (notes, schedule) are
    # registered before parameterised /patients/{id} sub-resources, but the
    # prefixes are disjoint so order is not load-bearing here.
    app.include_router(health.router)
    app.include_router(patients.router)
    app.include_router(patient_documents.router)
    app.include_router(encounters.router)
    app.include_router(notes.router)
    app.include_router(consents.router)
    app.include_router(anamnesis.router)
    app.include_router(privacy.router)

    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
