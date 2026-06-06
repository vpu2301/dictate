"""Service-wide singletons for signing-service."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from audit import AuditWriter
from auth import JwksCache
from db import create_pool
from medical_kep import TrustStore

from .config import settings
from .providers import ProviderRegistry, build_registry
from .rate_limit import PublicVerifyRateLimiter

logger = logging.getLogger(__name__)


@dataclass
class ServiceState:
    jwks_cache: JwksCache
    app_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    public_verify_pool: asyncpg.Pool
    callback_writer_pool: asyncpg.Pool
    audit_writer: AuditWriter
    providers: ProviderRegistry
    trust_store: TrustStore
    rate_limiter: PublicVerifyRateLimiter


async def build_state() -> ServiceState:
    jwks_cache = JwksCache(
        issuer_to_url={settings.auth_issuer: settings.auth_jwks_url}
    )
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
    public_verify_pool = await create_pool(
        settings.db_public_verify_dsn,
        application_name=f"{settings.service_name}/public_verify",
        min_size=1,
        max_size=8,
    )
    callback_writer_pool = await create_pool(
        settings.db_callback_writer_dsn,
        application_name=f"{settings.service_name}/callback_writer",
        min_size=1,
        max_size=4,
    )

    trust_store = TrustStore.load_from_dir(
        Path(settings.trust_store_dir),
        include_test_ca=settings.trust_store_include_test_ca,
    )

    from redis.asyncio import Redis

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    rate_limiter = PublicVerifyRateLimiter(
        redis, per_minute=settings.public_verify_rate_per_minute
    )

    providers = build_registry()

    return ServiceState(
        jwks_cache=jwks_cache,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        public_verify_pool=public_verify_pool,
        callback_writer_pool=callback_writer_pool,
        audit_writer=AuditWriter(audit_writer_pool),
        providers=providers,
        trust_store=trust_store,
        rate_limiter=rate_limiter,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.providers.aclose()
    await state.jwks_cache.aclose()
    await state.app_pool.close()
    await state.audit_writer_pool.close()
    await state.public_verify_pool.close()
    await state.callback_writer_pool.close()
