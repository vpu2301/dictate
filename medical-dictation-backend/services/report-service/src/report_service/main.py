"""report-service entry point (sprint-06 templates + sprint-08 reports)."""

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
    audio_clips,
    health,
    icd10,
    phi_access,
    reports,
    reports_amend,
    reports_audio,
    reports_diff,
    reports_drafts,
    reports_from_transcript,
    reports_lifecycle,
    reports_pdf,
    reports_search,
    reports_sign,
    reports_synthesis,
    reports_versions,
    search_tips,
    synonyms,
    templates,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="report-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )
    state = await build_state()
    app.state.svc = state
    install_state(state)
    # Sprint 16: idle-draft cleanup hosted in-process (ADR-0041). Off by
    # default in dev; production flips MDX_BACKGROUND_JOBS. The job is
    # idempotent and also runs as a CLI for external cron.
    jobs: list[asyncio.Task[None]] = []
    if settings.background_jobs_enabled and not settings.testing:
        from datetime import timedelta

        from observability import run_periodic

        from .jobs import idle_draft_cleanup

        async def _idle_draft_iteration() -> dict[str, int]:
            return await idle_draft_cleanup.run_for_all_tenants(
                app_pool=state.app_pool,
                audit_writer=state.audit_writer,
                idle_for=timedelta(days=settings.idle_draft_days),
            )

        jobs.append(
            asyncio.create_task(
                run_periodic(
                    job_name="idle_draft_cleanup",
                    interval_seconds=settings.background_jobs_interval_s,
                    fn=_idle_draft_iteration,
                ),
                name="idle-draft-cleanup",
            )
        )
    logger.info(
        "report-service.started",
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
        logger.info("report-service.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Report Service",
        description="Sprint-06 templates + sprint-08 reports / versions / diff / search.",
        version="0.8.0",
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
    app.include_router(templates.router)
    app.include_router(icd10.router)
    app.include_router(phi_access.router)
    # Search route must be registered BEFORE the parameterised ``{report_id}``
    # routes so ``/v1/reports/search`` matches the search handler rather than
    # ``GET /v1/reports/{report_id}``.
    app.include_router(reports_search.router)
    # BEFORE reports.router: its literal paths (/from-transcript,
    # /by-source-job) must win over reports' /{report_id} catch-all.
    app.include_router(reports_from_transcript.router)
    app.include_router(reports.router)
    app.include_router(reports_drafts.router)
    app.include_router(reports_lifecycle.router)
    app.include_router(reports_amend.router)
    app.include_router(reports_sign.router)
    app.include_router(reports_diff.router)
    app.include_router(reports_versions.router)
    app.include_router(reports_pdf.router)
    app.include_router(reports_synthesis.router)
    # Sprint 15: audio replay (ADR-0037). No ordering hazard: the
    # multi-segment sections path can't be swallowed by /{report_id}.
    app.include_router(reports_audio.router)
    app.include_router(audio_clips.router)
    # Sprint 15: query expansion surfaces (ADR-0038).
    app.include_router(search_tips.router)
    app.include_router(synonyms.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
