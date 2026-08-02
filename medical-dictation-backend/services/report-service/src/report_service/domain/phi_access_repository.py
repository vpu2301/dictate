"""Break-glass grants — RLS-scoped SQL over ``phi_access_requests`` and
the step-up tickets auth-service mints (migration 0056).

Every function takes a connection already bound to ``app.tenant_id`` via
:func:`db.tenant_connection`, so none of them spell out a tenant
predicate for isolation — Postgres RLS is the enforcement, per ADR-0006.

The one rule worth stating twice: :func:`consume_reauth_ticket` is the
security boundary of this module. It must stay a single atomic statement.
A read-then-write would let two concurrent requests both observe an
unconsumed ticket and both mint a grant, turning "single-use" into
"single-use per race".
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

_COLUMNS = """
    id, tenant_id, requested_by, resource_kind, resource_id, patient_id,
    reason_code, reason_note, status, granted_at, expires_at,
    revoked_at, revoked_by, use_count, last_used_at, created_at
"""


async def consume_reauth_ticket(
    conn: asyncpg.Connection,
    *,
    subject_sub: UUID,
    ticket_hash: bytes,
    purpose: str,
) -> bool:
    """Redeem a step-up ticket exactly once. ``True`` iff this call was the
    one that consumed it.

    All four predicates matter and none is redundant:

    ``ticket_hash``   the secret itself
    ``subject_sub``   binds it to the caller, so a ticket lifted from
                      another user's response body is worthless
    ``purpose``       binds it to the act it was minted for
    ``consumed_at``   single use
    ``expires_at``    freshness — "typed their password JUST now"

    The ``UPDATE … RETURNING`` is atomic: the row lock serialises
    concurrent redemptions and only the first sees ``consumed_at IS NULL``.
    """
    row = await conn.fetchrow(
        """
        UPDATE auth_reauth_tickets
           SET consumed_at = now()
         WHERE ticket_hash = $1
           AND subject_sub = $2
           AND purpose     = $3
           AND consumed_at IS NULL
           AND expires_at  > now()
        RETURNING id
        """,
        ticket_hash,
        subject_sub,
        purpose,
    )
    return row is not None


async def create_grant(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    requested_by: UUID,
    resource_kind: str = "report",
    resource_id: UUID,
    patient_id: UUID | None,
    reason_code: str,
    reason_note: str,
    expires_at: datetime,
) -> asyncpg.Record:
    return await conn.fetchrow(
        f"""
        INSERT INTO phi_access_requests
            (tenant_id, requested_by, resource_kind, resource_id,
             patient_id, reason_code, reason_note, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING {_COLUMNS}
        """,
        tenant_id,
        requested_by,
        resource_kind,
        resource_id,
        patient_id,
        reason_code,
        reason_note,
        expires_at,
    )


async def find_live_grant(
    conn: asyncpg.Connection,
    *,
    user_sub: UUID,
    resource_id: UUID,
    resource_kind: str = "report",
) -> asyncpg.Record | None:
    """The authorization lookup: does this user hold an unexpired,
    unrevoked grant on this resource right now?

    ``resource_kind`` matters since 0061: a patient-kind grant must never
    open a report that happens to share the UUID, and vice versa.

    Ordered by ``expires_at DESC`` so a re-request that widens the window
    wins over an older grant that is about to lapse — otherwise a user who
    just re-authenticated could still be cut off mid-read.
    """
    return await conn.fetchrow(
        f"""
        SELECT {_COLUMNS}
          FROM phi_access_requests
         WHERE requested_by  = $1
           AND resource_id   = $2
           AND resource_kind = $3
           AND status        = 'granted'
           AND expires_at    > now()
         ORDER BY expires_at DESC
         LIMIT 1
        """,
        user_sub,
        resource_id,
        resource_kind,
    )


async def record_grant_use(conn: asyncpg.Connection, *, grant_id: UUID) -> None:
    """Stamp a read against its grant.

    "Requested access and never opened it" and "read it eleven times" are
    different facts, and only the second one is a pattern. The audit chain
    records each read too; this counter is what makes the oversight LIST
    answerable without replaying the chain.
    """
    await conn.execute(
        """
        UPDATE phi_access_requests
           SET use_count = use_count + 1,
               last_used_at = now()
         WHERE id = $1
        """,
        grant_id,
    )


async def get_grant(conn: asyncpg.Connection, *, grant_id: UUID) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_COLUMNS} FROM phi_access_requests WHERE id = $1",
        grant_id,
    )


async def list_grants(
    conn: asyncpg.Connection,
    *,
    requested_by: UUID | None = None,
    resource_id: UUID | None = None,
    active_only: bool = False,
    limit: int = 50,
) -> list[asyncpg.Record]:
    where: list[str] = []
    args: list[object] = []
    if requested_by is not None:
        args.append(requested_by)
        where.append(f"requested_by = ${len(args)}")
    if resource_id is not None:
        args.append(resource_id)
        where.append(f"resource_id = ${len(args)}")
    if active_only:
        where.append("status = 'granted' AND expires_at > now()")
    args.append(limit)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return list(
        await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM phi_access_requests
            {where_sql}
            ORDER BY granted_at DESC, id DESC
            LIMIT ${len(args)}
            """,
            *args,
        )
    )


async def revoke_grant(
    conn: asyncpg.Connection, *, grant_id: UUID, revoked_by: UUID
) -> asyncpg.Record | None:
    """Close an open grant early. Returns ``None`` if it was not open —
    already revoked, or never existed — so the caller can tell "revoked
    it" from "nothing to revoke" without a second query.

    Expired grants are deliberately still revocable: the status is the
    reviewer's verdict on whether the reason held up, and that verdict is
    worth recording after the window has closed.
    """
    return await conn.fetchrow(
        f"""
        UPDATE phi_access_requests
           SET status = 'revoked',
               revoked_at = now(),
               revoked_by = $2
         WHERE id = $1
           AND status = 'granted'
        RETURNING {_COLUMNS}
        """,
        grant_id,
        revoked_by,
    )
