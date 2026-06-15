"""Service-wide singletons: JWKS cache, DB pools, audit components.

Created at process start (in main.py's lifespan). Routers consume them
via :func:`auth_service.deps.get_state`.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from audit import AuditVerifier, AuditWriter
from auth import JwksCache
from db import create_pool

from .config import settings
from .jwks_metrics import instrument_jwks_cache
from .keycloak_client import KeycloakClient


@dataclass
class ServiceState:
    """Container for runtime singletons. Stored on ``app.state.svc``."""

    jwks_cache: JwksCache
    app_pool: asyncpg.Pool
    tenant_writer_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    audit_reader_pool: asyncpg.Pool
    audit_writer: AuditWriter
    audit_verifier: AuditVerifier
    keycloak: KeycloakClient


async def build_state() -> ServiceState:
    """Construct every async resource the service needs."""
    jwks_cache = JwksCache(issuer_to_url={settings.auth_issuer: settings.auth_jwks_url})
    instrument_jwks_cache(jwks_cache)

    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name=f"{settings.service_name}/app",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    tenant_writer_pool = await create_pool(
        settings.db_tenant_writer_dsn,
        application_name=f"{settings.service_name}/tenant_writer",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    audit_writer_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name=f"{settings.service_name}/audit_writer",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    audit_reader_pool = await create_pool(
        settings.db_audit_reader_dsn,
        application_name=f"{settings.service_name}/audit_reader",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )

    keycloak = KeycloakClient(
        base_url=settings.keycloak_base_url,
        realm=settings.keycloak_realm,
        login_client_id=settings.keycloak_login_client_id,
        login_client_secret=settings.keycloak_login_client_secret,
        admin_client_id=settings.keycloak_admin_client_id,
        admin_client_secret=settings.keycloak_admin_client_secret,
    )

    return ServiceState(
        jwks_cache=jwks_cache,
        app_pool=app_pool,
        tenant_writer_pool=tenant_writer_pool,
        audit_writer_pool=audit_writer_pool,
        audit_reader_pool=audit_reader_pool,
        audit_writer=AuditWriter(audit_writer_pool),
        audit_verifier=AuditVerifier(audit_reader_pool),
        keycloak=keycloak,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.jwks_cache.aclose()
    await state.app_pool.close()
    await state.tenant_writer_pool.close()
    await state.audit_writer_pool.close()
    await state.audit_reader_pool.close()
    await state.keycloak.aclose()
