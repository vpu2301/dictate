"""Tenant-scoped connection acquisition.

``tenant_connection`` is the single sanctioned way to obtain a DB connection
in any service. It enforces the row-level-security (RLS) contract by setting
``app.tenant_id`` as a transaction-local config and committing only on a
clean exit. Bypassing it (e.g. raw ``pool.acquire``) circumvents tenant
isolation and is forbidden — see ADR-0004.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)

# Detects nested tenant_connection on the same task — usually a caller bug.
_in_tenant_scope: ContextVar[UUID | None] = ContextVar("_in_tenant_scope", default=None)


def _coerce_tenant_id(tenant_id: UUID | str) -> UUID:
    if isinstance(tenant_id, UUID):
        return tenant_id
    if not isinstance(tenant_id, str):
        raise TypeError(f"tenant_id must be UUID or str, got {type(tenant_id).__name__}")
    try:
        return UUID(tenant_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"tenant_id is not a valid UUID: {tenant_id!r}") from exc


@asynccontextmanager
async def tenant_connection(
    pool: asyncpg.Pool, tenant_id: UUID | str
) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a tenant-scoped DB connection.

    The connection is wrapped in a transaction. ``app.tenant_id`` is set as
    a transaction-local config (the ``true`` flag in ``set_config``) so it
    is automatically cleared at COMMIT or ROLLBACK and cannot leak into a
    subsequent connection-pool reuse. The transaction commits on clean exit
    and rolls back on exception.

    Raises:
        ValueError: ``tenant_id`` is not a valid UUID.
        TypeError: ``tenant_id`` is the wrong type.
    """
    tid = _coerce_tenant_id(tenant_id)

    parent = _in_tenant_scope.get()
    if parent is not None:
        logger.warning(
            "Nested tenant_connection detected (parent=%s, child=%s). "
            "This is almost always a caller bug — flatten the call chain.",
            parent,
            tid,
        )

    token = _in_tenant_scope.set(tid)
    try:
        async with pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                # The third argument (true) makes the setting transaction-local.
                # Do NOT use SET LOCAL with parameter binding — Postgres rejects
                # parameters in SET. set_config(name, value, is_local) is the
                # safe form and is parameterised.
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(tid),
                )
                yield conn
            except Exception:
                await tx.rollback()
                raise
            else:
                await tx.commit()
    finally:
        _in_tenant_scope.reset(token)
