"""Every SQL statement the password-recovery flow issues.

Follows the ``tenants_repository`` shape already in this service: plain
async functions taking an ``asyncpg`` connection, so a router can be
unit-tested by monkeypatching this module wholesale.

Two of these functions run on an UNSCOPED connection and say so loudly
in their names, because they must: redemption starts from a token and a
browser with no session, so there is no ``app.tenant_id`` to scope by
yet. Both delegate to the SECURITY DEFINER functions from migration
0076 rather than reading the tables directly — the function is the
narrow, auditable hole in RLS, and widening the pool's rights would be a
much larger one.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

# ── Account resolution (tenant-blind) ────────────────────────────────


async def resolve_account_by_email(
    conn: asyncpg.Connection, *, email: str
) -> asyncpg.Record | None:
    """Email → (tenant_id, subject_sub, email, display_name, status).

    Tenant-blind by necessity: the caller has an email address typed
    into a logged-out form and nothing else.
    """
    return await conn.fetchrow(
        "SELECT * FROM public.resolve_account_for_password_reset($1)", email
    )


async def peek_token(
    conn: asyncpg.Connection, *, token_hash: bytes, purpose: str
) -> asyncpg.Record | None:
    """Resolve a token to (tenant, subject) WITHOUT spending it.

    Exists so the reset handler can judge the proposed password — which
    needs the account's own email and name — before committing the
    single use. See migration 0076 for why that order matters.
    """
    return await conn.fetchrow(
        "SELECT * FROM public.peek_password_reset_token($1, $2)",
        token_hash,
        purpose,
    )


async def consume_token(
    conn: asyncpg.Connection, *, token_hash: bytes, purpose: str
) -> asyncpg.Record | None:
    """Atomically claim a token. ``None`` if unknown, spent, or expired.

    The three failure modes are deliberately indistinguishable to the
    caller: telling a stranger which one applied would confirm that a
    token existed at all.
    """
    return await conn.fetchrow(
        "SELECT * FROM public.consume_password_reset_token($1, $2)",
        token_hash,
        purpose,
    )


# ── Token issuance (tenant-scoped) ───────────────────────────────────


async def insert_token(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    subject_sub: UUID,
    token_hash: bytes,
    purpose: str,
    expires_at: datetime,
    requested_ip_hash: str,
) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO auth_password_reset_tokens
            (tenant_id, subject_sub, token_hash, purpose,
             expires_at, requested_ip_hash)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        tenant_id,
        subject_sub,
        token_hash,
        purpose,
        expires_at,
        requested_ip_hash,
    )
    return UUID(str(row["id"]))


async def spend_all_tokens(conn: asyncpg.Connection, *, subject_sub: UUID) -> int:
    """Spend every live token for a user. Returns how many were spent.

    Called after a successful reset and after a lockdown. A password
    that has just changed must not leave a second, still-live link in an
    inbox somewhere — that link would be a standing key to an account
    whose owner believes they have just secured it.
    """
    result = await conn.execute(
        """
        UPDATE auth_password_reset_tokens
           SET consumed_at = now()
         WHERE subject_sub = $1 AND consumed_at IS NULL
        """,
        subject_sub,
    )
    # asyncpg returns the command tag, e.g. "UPDATE 3".
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


async def sweep_dead_tokens(conn: asyncpg.Connection) -> None:
    """Opportunistic cleanup of this tenant's spent/expired tokens.

    Folded into the issuance path so the table needs no cron of its own —
    the same trick ``reauth`` uses. Rows carry no PHI and, once spent or
    expired, no authority, so there is nothing to retain.
    """
    await conn.execute(
        """
        DELETE FROM auth_password_reset_tokens
         WHERE expires_at < now() - INTERVAL '1 day'
            OR (consumed_at IS NOT NULL
                AND consumed_at < now() - INTERVAL '1 day')
        """
    )


# ── Outbox ───────────────────────────────────────────────────────────


async def enqueue_mail(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    subject_sub: UUID,
    kind: str,
    lang: str,
    to_address: str,
    render_fields: dict[str, Any],
    secret_fields: dict[str, Any],
) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO auth_mail_outbox
            (tenant_id, subject_sub, kind, lang, to_address,
             render_fields, secret_fields)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
        RETURNING id
        """,
        tenant_id,
        subject_sub,
        kind,
        lang,
        to_address,
        json.dumps(render_fields),
        json.dumps(secret_fields),
    )
    return UUID(str(row["id"]))


async def claim_due_mail(conn: asyncpg.Connection) -> asyncpg.Record | None:
    """Take one due row, locked for the life of the transaction.

    ``SKIP LOCKED`` so two workers never contend, and one row at a time
    because the caller wraps each in its own transaction — see the
    worker for why a batch-per-transaction loses mail.
    """
    return await conn.fetchrow(
        """
        SELECT id, tenant_id, subject_sub, kind, lang, to_address,
               render_fields, secret_fields, attempt_count
          FROM auth_mail_outbox
         WHERE status = 'pending' AND next_attempt_at <= now()
         ORDER BY next_attempt_at
         FOR UPDATE SKIP LOCKED
         LIMIT 1
        """
    )


async def mark_sent(
    conn: asyncpg.Connection, *, mail_id: UUID, provider_message_id: str
) -> None:
    """Record the send AND destroy the token-bearing variables.

    The two happen in one statement on purpose: any path that marks a
    row sent without clearing ``secret_fields`` would leave a live reset
    URL in the database indefinitely. Migration 0076's CHECK makes the
    omission an error rather than a slow leak.
    """
    await conn.execute(
        """
        UPDATE auth_mail_outbox
           SET status = 'sent',
               sent_at = now(),
               secret_fields = NULL,
               provider_message_id = $2,
               last_error = ''
         WHERE id = $1
        """,
        mail_id,
        provider_message_id,
    )


async def mark_dead(conn: asyncpg.Connection, *, mail_id: UUID, error: str) -> None:
    await conn.execute(
        """
        UPDATE auth_mail_outbox
           SET status = 'dead',
               secret_fields = NULL,
               last_error = $2
         WHERE id = $1
        """,
        mail_id,
        error[:1000],
    )


async def mark_retry(
    conn: asyncpg.Connection, *, mail_id: UUID, error: str, delay_seconds: float
) -> None:
    await conn.execute(
        """
        UPDATE auth_mail_outbox
           SET attempt_count = attempt_count + 1,
               next_attempt_at = now() + make_interval(secs => $3),
               last_error = $2
         WHERE id = $1
        """,
        mail_id,
        error[:1000],
        float(delay_seconds),
    )


async def tenants_with_due_mail(conn: asyncpg.Connection) -> list[UUID]:
    """Which tenants currently have deliverable mail.

    The worker holds no tenant context of its own and the outbox is
    RLS-scoped, so the drain loop asks this first and then opens a
    properly scoped connection per tenant.

    Goes through the SECURITY DEFINER function rather than reading the
    table: an ``app_role`` connection with no ``app.tenant_id`` set sees
    zero rows through RLS (the policy compares against a NULL), so a
    direct SELECT here silently returns nothing and no mail is ever
    sent. Found by running it, not by a unit test — the fakes had no
    RLS.
    """
    rows = await conn.fetch("SELECT * FROM public.tenants_with_due_auth_mail($1)", 100)
    return [UUID(str(r["tenant_id"])) for r in rows]


# ── Security-activity projection ─────────────────────────────────────


async def record_password_event(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    subject_sub: UUID,
    kind: str,
    via: str,
    ip_hash: str,
    client_label: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO auth_password_events
            (tenant_id, subject_sub, kind, via, ip_hash, client_label)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        tenant_id,
        subject_sub,
        kind,
        via,
        ip_hash,
        client_label,
    )


async def recent_password_events(
    conn: asyncpg.Connection, *, subject_sub: UUID, limit: int = 20
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            """
            SELECT kind, via, client_label, created_at
              FROM auth_password_events
             WHERE subject_sub = $1
             ORDER BY created_at DESC
             LIMIT $2
            """,
            subject_sub,
            limit,
        )
    )
