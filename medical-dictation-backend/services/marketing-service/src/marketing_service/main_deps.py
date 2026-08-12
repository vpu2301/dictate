"""Service state: the pool and the mail provider. Nothing else.

No JWKS cache, no audit writer, no Redis. This service authenticates
nobody and holds no tenant data, and every dependency it does not have
is one that cannot take the public demo form down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from db import create_pool

from .adapters.email import EmailProvider, build_provider
from .config import settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ServiceState:
    app_pool: asyncpg.Pool
    email_provider: EmailProvider


async def build_state() -> ServiceState:
    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name=f"{settings.service_name}/app",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    return ServiceState(
        app_pool=app_pool,
        email_provider=build_provider(
            kind=settings.email_provider,
            is_production=settings.is_production,
            host=settings.smtp_host,
            port=settings.smtp_port,
            from_address=settings.email_from,
            from_name=settings.email_from_name,
            use_tls=settings.smtp_use_tls,
            username=settings.smtp_username,
            password=settings.smtp_password,
        ),
    )


async def teardown_state(state: ServiceState) -> None:
    await state.email_provider.aclose()
    await state.app_pool.close()
