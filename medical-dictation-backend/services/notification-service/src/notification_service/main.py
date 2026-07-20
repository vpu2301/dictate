"""notification-service entry point."""

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
from .routers import feed, health, preferences, ws
from .ws.handler import make_fanout_handler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="notification-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )
    if settings.auth_bypass_dev:
        logger.warning(
            "AUTH_BYPASS_DEV=true — authentication is disabled. "
            "This must never be set in staging or production.",
        )

    state = await build_state()
    app.state.svc = state
    install_state(state)

    # Gauges the sprint-12 alerts read. Registered here, once, with
    # callbacks over live state.
    from .jobs.digest import last_success_unix
    from .metrics import register_gauges

    _pending: dict[str, int] = {"value": 0}

    def _stream_pending(_options):  # noqa: ANN001, ANN202
        from opentelemetry.metrics import Observation

        return [Observation(_pending["value"])]

    def _digest_last_run(_options):  # noqa: ANN001, ANN202
        from opentelemetry.metrics import Observation

        return [Observation(last_success_unix())]

    def _connected_sockets(_options):  # noqa: ANN001, ANN202
        from opentelemetry.metrics import Observation

        return [Observation(state.socket_registry.connected_socket_count())]

    register_gauges(
        stream_pending_cb=_stream_pending,
        digest_last_run_cb=_digest_last_run,
        connected_sockets_cb=_connected_sockets,
    )
    state.stream_pending_ref = _pending

    tasks: list[asyncio.Task[None]] = []
    if not settings.testing:
        # Every worker subscribes, so a notification materialised here
        # reaches a socket pinned to a sibling worker (ADR-0030).
        state.fanout.set_handler(make_fanout_handler(state))
        await state.fanout.start()

        if settings.ingest_enabled:
            from .ingest.consumer import run_forever as ingest_forever

            tasks.append(
                asyncio.create_task(
                    ingest_forever(
                        app_pool=state.app_pool,
                        audit_writer=state.audit_writer,
                        redis=state.redis,
                    ),
                    name="notification-ingest",
                )
            )

        if settings.background_jobs_enabled:
            from .delivery.worker import run_forever as delivery_forever

            tasks.append(
                asyncio.create_task(
                    delivery_forever(state=state), name="notification-delivery"
                )
            )

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await teardown_state(state)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Notification Service",
        description=(
            "Sprint-12 notification fan-out: in-app feed, WebSocket push "
            "(medical-notifications.v1), and email."
        ),
        version="0.12.0",
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
    app.include_router(feed.router)
    app.include_router(preferences.router)
    app.include_router(ws.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
