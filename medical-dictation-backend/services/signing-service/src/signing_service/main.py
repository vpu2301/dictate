"""signing-service entry point."""

from __future__ import annotations

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
from .middleware import (
    PublicVerifySecurityHeadersMiddleware,
    RequestIDMiddleware,
)
from .routers import callbacks, certificates, health, inline, sessions, uploads, verify

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    bootstrap(
        settings.service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        log_level=settings.log_level,
        deployment_environment=settings.environment,
        package_name="signing-service",
        disable_otel=settings.testing or settings.otel_sdk_disabled,
    )
    # Sprint 16: KMS-sourced system HMAC keys. Fail-closed — if the flag is
    # on and Vault can't serve the keys, the service must not start with
    # the "00"*32 env placeholders (they'd silently HMAC under a dev key).
    if settings.hmac_keys_from_vault:
        from crypto import fetch_kv_secrets

        kv = await fetch_kv_secrets(
            addr=settings.vault_addr,
            token=settings.vault_token,
            path=settings.vault_hmac_kv_path,
            mount=settings.vault_kv_mount,
        )
        try:
            settings.signer_ipn_hmac_key_hex = kv["signer_ipn_hmac_key"]
            settings.public_verify_ip_hmac_key_hex = kv["public_verify_ip_hmac_key"]
        except KeyError as exc:
            raise RuntimeError(
                f"Vault KV {settings.vault_hmac_kv_path!r} is missing field {exc}. "
                "See docs/runbooks/kms.md § hmac-keys."
            ) from exc
        logger.info("signing.hmac_keys_sourced_from_vault")
    state = await build_state()
    app.state.svc = state
    install_state(state)
    logger.info(
        "signing-service.started",
        extra={"service": settings.service_name, "env": settings.environment},
    )
    try:
        yield
    finally:
        await teardown_state(state)
        logger.info("signing-service.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Signing Service",
        description="Sprint-09 KEP signing + public /verify.",
        version="0.9.0",
        openapi_version="3.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.add_middleware(PublicVerifySecurityHeadersMiddleware)
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    # CORS for the SPA. allow_credentials=True is required so the browser sends
    # the HttpOnly `mdx_rt` cookie on cross-origin XHR; that forbids a wildcard
    # origin, so origins are an explicit allow-list (mirror auth-service A3).
    # Note: the public /verify routes are origin-agnostic; this only governs
    # browser XHR from the SPA.
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
    app.include_router(sessions.router)
    app.include_router(inline.router)
    app.include_router(certificates.router)
    app.include_router(uploads.router)
    app.include_router(callbacks.router)
    app.include_router(verify.router)
    FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
