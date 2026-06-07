"""asyncpg connection pool factory with safe defaults.

Defaults chosen so that this pool can be placed behind pgbouncer/pgcat in
production (Sprint 16) without further changes:

- ``statement_cache_size=0`` — required when using a transaction-mode pooler.
- ``application_name`` — set so DB observability can attribute traffic.
"""

from __future__ import annotations

from typing import Any

import asyncpg


async def create_pool(
    dsn: str,
    *,
    application_name: str,
    min_size: int = 1,
    max_size: int = 10,
    command_timeout: float = 30.0,
    server_settings: dict[str, str] | None = None,
) -> asyncpg.Pool:
    """Create an asyncpg connection pool with safe production defaults."""
    settings: dict[str, str] = {"application_name": application_name}
    if server_settings:
        settings.update(server_settings)

    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
        statement_cache_size=0,
        server_settings=settings,
    )
    if pool is None:  # pragma: no cover  — asyncpg.create_pool returns None only on lazy init
        raise RuntimeError("asyncpg.create_pool returned None")
    return _typed_pool(pool)


def _typed_pool(pool: Any) -> asyncpg.Pool:
    return pool
