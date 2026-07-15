"""Service-wide singletons for core-service."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from audit import AuditWriter
from auth import JwksCache
from crypto import Envelope, FileMasterKeyProvider, TenantKekRepository
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
    # Envelope crypto for raw-ІПН retention; wired only when
    # PATIENT_IPN_RAW_ENABLED=true (DPO-gated, default off).
    envelope: Envelope | None = None
    crypto_pool: asyncpg.Pool | None = None


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

    envelope: Envelope | None = None
    crypto_pool: asyncpg.Pool | None = None
    if settings.patient_ipn_raw_enabled:
        crypto_pool = await create_pool(
            settings.db_crypto_writer_dsn,
            application_name=f"{settings.service_name}/crypto_writer",
            min_size=1,
            max_size=2,
        )
        master = FileMasterKeyProvider(path=settings.master_key_path)
        await master.startup_self_check()
        kek_repo = TenantKekRepository(pool=crypto_pool, master_key_provider=master)
        envelope = Envelope(master_key_provider=master, kek_repository=kek_repo)
        logger.info("raw-ІПН retention enabled: envelope crypto wired")

    return ServiceState(
        jwks_cache=jwks_cache,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        audit_writer=audit_writer,
        envelope=envelope,
        crypto_pool=crypto_pool,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.jwks_cache.aclose()
    await state.app_pool.close()
    await state.audit_writer_pool.close()
    if state.crypto_pool is not None:
        await state.crypto_pool.close()
