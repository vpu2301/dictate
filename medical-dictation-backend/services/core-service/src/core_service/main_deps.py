"""Service-wide singletons for core-service."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from audit import AuditWriter
from auth import JwksCache
from db import create_pool

from .config import settings

logger = logging.getLogger(__name__)


@dataclass
class ServiceState:
    """Singletons wired at startup; reached by routers via deps.get_state()."""

    jwks_cache: JwksCache
    app_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    audit_writer: AuditWriter


async def build_state() -> ServiceState:
    jwks_cache = JwksCache(issuer_to_url={settings.auth_issuer: settings.auth_jwks_url})

    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name=f"{settings.service_name}/app",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    audit_writer_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name=f"{settings.service_name}/audit_writer",
        min_size=1,
        max_size=4,
    )
    audit_writer = AuditWriter(audit_writer_pool)

    return ServiceState(
        jwks_cache=jwks_cache,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        audit_writer=audit_writer,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.jwks_cache.aclose()
    await state.app_pool.close()
    await state.audit_writer_pool.close()
