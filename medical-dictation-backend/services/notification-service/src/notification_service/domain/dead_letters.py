"""Forensic record of items that can never be delivered.

Lives in `domain` rather than beside either writer: BOTH the ingest
consumer (undecodable envelope) and the delivery worker (retries
exhausted) need it, and having delivery reach into `ingest` for it
inverted the layering — dead-lettering is shared infrastructure, not an
ingest detail.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import asyncpg

from db import tenant_connection

logger = logging.getLogger(__name__)


async def write_dead_letter(
    app_pool: asyncpg.Pool,
    *,
    source: str,
    envelope: dict[str, Any],
    error: str,
    notification_id: UUID | None = None,
    outbox_id: UUID | None = None,
    channel: str = "",
    attempt_count: int = 0,
) -> None:
    """Record a permanently-failed item.

    Scoping depends on what we know. When the tenant parsed, the write
    goes through `tenant_connection` like every other write — the RLS
    predicate demands a matching `app.tenant_id`, so an unscoped
    connection would be refused. When the envelope was too malformed to
    yield a tenant, the row is written NULL-tenant on a plain
    connection, which the policy admits precisely so that the evidence
    of a broken producer survives.
    """
    tenant_id: UUID | None = None
    raw_tenant = envelope.get("tenant_id")
    if isinstance(raw_tenant, str):
        try:
            tenant_id = UUID(raw_tenant)
        except ValueError:
            tenant_id = None

    event_id: UUID | None = None
    raw_event = envelope.get("event_id")
    if isinstance(raw_event, str):
        try:
            event_id = UUID(raw_event)
        except ValueError:
            event_id = None

    sql = """
        INSERT INTO audit.notification_dead_letters
            (tenant_id, source, notification_id, outbox_id, channel,
             event_id, attempt_count, last_error, envelope)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
    """
    args = (
        tenant_id,
        source,
        notification_id,
        outbox_id,
        channel,
        event_id,
        attempt_count,
        error[:2000],
        json.dumps(envelope, ensure_ascii=False, default=str),
    )

    if tenant_id is not None:
        async with tenant_connection(app_pool, tenant_id) as conn:
            await conn.execute(sql, *args)
    else:
        async with app_pool.acquire() as conn:
            await conn.execute(sql, *args)

    logger.warning(
        "ingest.dead_letter",
        extra={"source": source, "tenant_id": str(tenant_id), "error": error[:200]},
    )
