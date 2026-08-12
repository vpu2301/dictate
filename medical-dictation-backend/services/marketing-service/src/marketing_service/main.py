"""marketing-service entry point."""

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
from .routers import demo, health

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="marketing-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )

    state = await build_state()
    app.state.svc = state
    install_state(state)

    tasks: list[asyncio.Task[None]] = []
    if not settings.testing and settings.background_jobs_enabled:
        from .delivery.worker import run_forever as delivery_forever

        tasks.append(
            asyncio.create_task(delivery_forever(state=state), name="marketing-delivery")
        )

        if settings.booking_watch_enabled:
            if not (settings.imap_username and settings.imap_password):
                # Loud, and it does not start. A watcher that runs
                # without credentials logs a failure every two minutes
                # forever and looks like an outage; one that refuses to
                # start says what is actually wrong, once.
                logger.error(
                    "Booking watch is enabled but MDX_DEMO_IMAP_USERNAME/"
                    "MDX_DEMO_IMAP_PASSWORD are unset — booking confirmations "
                    "will NOT be sent."
                )
            else:
                from .jobs.booking_watch import run_forever as watch_forever

                tasks.append(
                    asyncio.create_task(
                        watch_forever(state=state), name="marketing-booking-watch"
                    )
                )
        else:
            logger.warning(
                "MDX_DEMO_WATCH_ENABLED is off — acknowledgement mail will be "
                "sent, but bookings will not be detected and no confirmation "
                "mail will go out."
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
        title="Marketing Service",
        description=(
            "Public demo-booking funnel: request acknowledgement and, once a "
            "slot is taken on the appointment page, booking confirmation."
        ),
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
        # No credentials: this service reads no cookie and issues no
        # session, and allowing them would widen CORS for nothing.
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        max_age=600,
    )
    app.include_router(health.router)
    app.include_router(demo.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
