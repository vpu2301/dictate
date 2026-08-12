"""Auth-service entry point.

Day-5 surface area:
- ``/healthz``, ``/readyz``
- ``/audit/events``  (paginated read; requires auditor or tenant_admin role)
- ``/audit/verify``  (chain verification; requires auditor or tenant_admin)

Days 6+ add login/refresh/logout/me/admin routers. The lifespan, DI, and
problem-detail handling established here are unchanged in later days.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from observability import bootstrap, register_exception_handlers

from .config import settings
from .delivery import worker as mail_worker
from .deps import install_state
from .main_deps import build_state, teardown_state
from .routers import (
    admin,
    audit,
    health,
    login,
    me,
    mfa,
    password,
    reauth,
    tenants,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="auth-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )

    state = await build_state()
    app.state.svc = state
    install_state(state)

    logger.info(
        "auth-service starting",
        extra={
            "service": settings.service_name,
            "env": settings.environment,
            "issuer": settings.auth_issuer,
        },
    )
    # Account-mail outbox drain. Only when the feature is on AND a
    # provider was built — a worker polling an outbox nothing writes to
    # is pure noise, and `testing` keeps it out of unit-test event loops.
    mail_task: asyncio.Task[None] | None = None
    if (
        settings.password_reset_enabled
        and settings.background_jobs_enabled
        and not settings.testing
        and state.email_provider is not None
    ):
        mail_task = asyncio.create_task(
            mail_worker.run_forever(
                app_pool=state.app_pool,
                provider=state.email_provider,
                reply_to=settings.auth_email_reply_to,
                interval_s=settings.mail_delivery_interval_s,
                batch_size=settings.mail_delivery_batch_size,
                max_attempts=settings.mail_delivery_max_attempts,
                backoff_base_s=settings.mail_delivery_backoff_base_s,
            )
        )
    elif settings.password_reset_enabled and not settings.testing:
        # Reset links would be minted and queued but never sent — the
        # user waits for mail that cannot arrive. Loud, because nothing
        # else in the system would surface it.
        logger.error(
            "auth.password.reset_enabled_but_no_mail_worker",
            extra={
                "background_jobs": settings.background_jobs_enabled,
                "has_provider": state.email_provider is not None,
            },
        )

    try:
        yield
    finally:
        if mail_task is not None:
            mail_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await mail_task
        await teardown_state(state)
        logger.info("auth-service shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Auth Service",
        description="Sprint-02 identity, tenant, and audit-read service.",
        version="0.2.0",
        openapi_version="3.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    register_exception_handlers(app)
    # CORS for the SPA (sprint A3). allow_credentials=True is required so the
    # browser sends/stores the HttpOnly `mdx_rt` cookie on cross-origin XHR;
    # that forbids a wildcard origin, so origins are an explicit allow-list.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["WWW-Authenticate"],
        max_age=600,
    )
    app.include_router(health.router)
    app.include_router(login.router)
    app.include_router(mfa.router)
    app.include_router(me.router)
    app.include_router(reauth.router)
    app.include_router(password.router)
    app.include_router(admin.router)
    app.include_router(tenants.router)
    app.include_router(audit.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
