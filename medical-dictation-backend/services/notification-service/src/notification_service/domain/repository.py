"""SQL for the notification tables.

Every function takes an already-tenant-scoped connection (from
``db.tenant_connection``). Nothing here re-checks the tenant in a WHERE
clause: RLS is the enforcement, and duplicating it in app code invites
the two to drift apart (ADR-0004/0007).
"""

from __future__ import annotations

import json
from datetime import datetime, time
from uuid import UUID

import asyncpg

from notification_events import Category, Channel, EmailMode

from .preferences import UserPreference, UserSettings

# ── recipients ──────────────────────────────────────────────────────


async def filter_to_tenant_members(
    conn: asyncpg.Connection, user_ids: tuple[UUID, ...]
) -> list[UUID]:
    """Keep only the ids that are active users OF THIS TENANT.

    This is the cross-tenant guard for producer-supplied recipient
    hints. Because the connection is tenant-scoped, RLS makes a hint
    naming a user in another tenant simply fail to match — a malicious
    or buggy producer cannot address someone else's user, and the fact
    materialises to nobody rather than leaking.
    """
    if not user_ids:
        return []
    rows = await conn.fetch(
        "SELECT sub FROM users WHERE sub = ANY($1::uuid[]) AND status = 'active'",
        list(user_ids),
    )
    return [r["sub"] for r in rows]


async def tenant_admin_ids(conn: asyncpg.Connection) -> list[UUID]:
    """Active tenant admins — the audience for operational alerts."""
    rows = await conn.fetch(
        "SELECT sub FROM users WHERE role = 'tenant_admin' AND status = 'active'"
    )
    return [r["sub"] for r in rows]


async def user_email(conn: asyncpg.Connection, user_id: UUID) -> str | None:
    row = await conn.fetchrow("SELECT email FROM users WHERE sub = $1", user_id)
    if row is None:
        return None
    email: str = row["email"]
    return email or None


# ── preferences ─────────────────────────────────────────────────────


async def load_preference(
    conn: asyncpg.Connection, *, user_id: UUID, category: Category
) -> UserPreference | None:
    """None means "never overridden" — the caller applies the catalog default."""
    row = await conn.fetchrow(
        "SELECT in_app_enabled, email_mode FROM notification_preferences "
        "WHERE user_id = $1 AND category = $2",
        user_id,
        str(category),
    )
    if row is None:
        return None
    return UserPreference(
        in_app_enabled=row["in_app_enabled"],
        email_mode=EmailMode(row["email_mode"]),
    )


async def load_all_preferences(
    conn: asyncpg.Connection, *, user_id: UUID
) -> dict[Category, UserPreference]:
    rows = await conn.fetch(
        "SELECT category, in_app_enabled, email_mode FROM notification_preferences "
        "WHERE user_id = $1",
        user_id,
    )
    return {
        Category(r["category"]): UserPreference(
            in_app_enabled=r["in_app_enabled"], email_mode=EmailMode(r["email_mode"])
        )
        for r in rows
    }


async def upsert_preference(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    user_id: UUID,
    category: Category,
    in_app_enabled: bool,
    email_mode: EmailMode,
) -> None:
    await conn.execute(
        """
        INSERT INTO notification_preferences
            (tenant_id, user_id, category, in_app_enabled, email_mode)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (tenant_id, user_id, category) DO UPDATE
           SET in_app_enabled = EXCLUDED.in_app_enabled,
               email_mode     = EXCLUDED.email_mode,
               updated_at     = now()
        """,
        tenant_id,
        user_id,
        str(category),
        in_app_enabled,
        str(email_mode),
    )


async def load_settings(conn: asyncpg.Connection, *, user_id: UUID) -> UserSettings:
    row = await conn.fetchrow(
        "SELECT timezone, quiet_hours_start, quiet_hours_end, digest_hour "
        "FROM notification_user_settings WHERE user_id = $1",
        user_id,
    )
    if row is None:
        return UserSettings()
    return UserSettings(
        timezone=row["timezone"],
        quiet_hours_start=row["quiet_hours_start"],
        quiet_hours_end=row["quiet_hours_end"],
        digest_hour=row["digest_hour"],
    )


async def upsert_settings(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    user_id: UUID,
    timezone: str,
    quiet_hours_start: time | None,
    quiet_hours_end: time | None,
    digest_hour: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO notification_user_settings
            (tenant_id, user_id, timezone, quiet_hours_start, quiet_hours_end, digest_hour)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (tenant_id, user_id) DO UPDATE
           SET timezone          = EXCLUDED.timezone,
               quiet_hours_start = EXCLUDED.quiet_hours_start,
               quiet_hours_end   = EXCLUDED.quiet_hours_end,
               digest_hour       = EXCLUDED.digest_hour,
               updated_at        = now()
        """,
        tenant_id,
        user_id,
        timezone,
        quiet_hours_start,
        quiet_hours_end,
        digest_hour,
    )


# ── notifications ───────────────────────────────────────────────────


async def insert_notification(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    recipient_user_id: UUID,
    category: Category,
    dedupe_key: str,
    title: str,
    body_text: str,
    deep_link: str,
    resource_type: str,
    resource_id: UUID | None,
    severity: str,
    render_fields: dict[str, str] | None = None,
) -> UUID | None:
    """Insert one row, idempotently.

    Returns the new id, or None if this (tenant, dedupe_key) already
    exists — i.e. the event is a redelivery. ON CONFLICT DO NOTHING makes
    that determination atomic; a SELECT-then-INSERT would let two
    consumers both miss and both insert.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO notifications
            (tenant_id, recipient_user_id, category, dedupe_key, title,
             body_text, deep_link, resource_type, resource_id, severity,
             render_fields)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
        ON CONFLICT (tenant_id, dedupe_key) DO NOTHING
        RETURNING id
        """,
        tenant_id,
        recipient_user_id,
        str(category),
        dedupe_key,
        title,
        body_text,
        deep_link,
        resource_type,
        resource_id,
        severity,
        json.dumps(render_fields or {}, ensure_ascii=False),
    )
    return None if row is None else row["id"]


async def bump_coalesced(
    conn: asyncpg.Connection, *, notification_id: UUID, title: str, body_text: str
) -> None:
    """Refresh a storm-coalescing row's text in place (E1)."""
    await conn.execute(
        "UPDATE notifications SET title = $2, body_text = $3, read_at = NULL "
        "WHERE id = $1",
        notification_id,
        title,
        body_text,
    )


async def notification_id_for_dedupe(
    conn: asyncpg.Connection, *, dedupe_key: str
) -> UUID | None:
    row = await conn.fetchrow(
        "SELECT id FROM notifications WHERE dedupe_key = $1", dedupe_key
    )
    return None if row is None else row["id"]


async def unread_count(conn: asyncpg.Connection, *, user_id: UUID) -> int:
    count: int = await conn.fetchval(
        "SELECT count(*) FROM notifications "
        "WHERE recipient_user_id = $1 AND read_at IS NULL",
        user_id,
    )
    return count


async def list_feed(
    conn: asyncpg.Connection,
    *,
    user_id: UUID,
    limit: int,
    before_created_at: datetime | None,
    before_id: UUID | None,
    unread_only: bool,
) -> list[asyncpg.Record]:
    """Unread-first, then newest-first, with a keyset cursor.

    The cursor is (created_at, id) rather than an OFFSET: a feed that
    gains rows while a client pages would otherwise duplicate or skip
    entries. `id` breaks ties when two rows share a timestamp.
    """
    clauses = ["recipient_user_id = $1"]
    params: list[object] = [user_id]

    if unread_only:
        clauses.append("read_at IS NULL")
    if before_created_at is not None and before_id is not None:
        params.extend([before_created_at, before_id])
        clauses.append(
            f"(created_at, id) < (${len(params) - 1}::timestamptz, ${len(params)}::uuid)"
        )

    params.append(limit)
    return await conn.fetch(
        f"""
        SELECT id, category, title, body_text, deep_link, resource_type,
               resource_id, severity, read_at, created_at
          FROM notifications
         WHERE {" AND ".join(clauses)}
         ORDER BY created_at DESC, id DESC
         LIMIT ${len(params)}
        """,
        *params,
    )


async def mark_read(
    conn: asyncpg.Connection, *, user_id: UUID, notification_id: UUID
) -> bool:
    """Idempotent: marking an already-read row again is a no-op success."""
    row = await conn.fetchrow(
        "UPDATE notifications SET read_at = coalesce(read_at, now()) "
        "WHERE id = $1 AND recipient_user_id = $2 RETURNING id",
        notification_id,
        user_id,
    )
    return row is not None


async def mark_all_read(conn: asyncpg.Connection, *, user_id: UUID) -> int:
    count: int = await conn.fetchval(
        "WITH updated AS ("
        "  UPDATE notifications SET read_at = now() "
        "   WHERE recipient_user_id = $1 AND read_at IS NULL RETURNING 1"
        ") SELECT count(*) FROM updated",
        user_id,
    )
    return count


# ── outbox ──────────────────────────────────────────────────────────


async def insert_outbox(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    notification_id: UUID,
    channel: Channel,
    status: str,
    suppressed_reason: str = "",
    next_attempt_at: datetime | None = None,
) -> UUID | None:
    """One row per (notification, channel). Idempotent on that pair."""
    row = await conn.fetchrow(
        """
        INSERT INTO notification_outbox
            (tenant_id, notification_id, channel, status,
             suppressed_reason, next_attempt_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (notification_id, channel) DO NOTHING
        RETURNING id
        """,
        tenant_id,
        notification_id,
        str(channel),
        status,
        suppressed_reason,
        next_attempt_at,
    )
    return None if row is None else row["id"]


async def claim_due_outbox(
    conn: asyncpg.Connection, *, limit: int
) -> list[asyncpg.Record]:
    """Claim due rows for this worker.

    FOR UPDATE SKIP LOCKED is what makes two delivery workers safe to run
    at once: each takes a disjoint set, and neither blocks on the other
    (E3). The lock is held for the enclosing transaction.
    """
    return await conn.fetch(
        """
        SELECT o.id, o.notification_id, o.channel, o.attempt_count,
               n.recipient_user_id, n.category, n.title, n.body_text,
               n.deep_link, n.severity, n.render_fields
          FROM notification_outbox o
          JOIN notifications n ON n.id = o.notification_id
         WHERE o.status = 'pending'
           AND o.next_attempt_at <= now()
         ORDER BY o.next_attempt_at
         LIMIT $1
           FOR UPDATE OF o SKIP LOCKED
        """,
        limit,
    )


async def mark_outbox_sent(
    conn: asyncpg.Connection, *, outbox_id: UUID, provider_message_id: str
) -> None:
    await conn.execute(
        "UPDATE notification_outbox "
        "   SET status = 'sent', next_attempt_at = NULL, last_error = '', "
        "       attempt_count = attempt_count + 1, provider_message_id = $2 "
        " WHERE id = $1",
        outbox_id,
        provider_message_id,
    )


async def mark_outbox_retry(
    conn: asyncpg.Connection, *, outbox_id: UUID, error: str, next_attempt_at: datetime
) -> None:
    await conn.execute(
        "UPDATE notification_outbox "
        "   SET status = 'pending', attempt_count = attempt_count + 1, "
        "       last_error = $2, next_attempt_at = $3 "
        " WHERE id = $1",
        outbox_id,
        error[:1000],
        next_attempt_at,
    )


async def mark_outbox_dead(
    conn: asyncpg.Connection, *, outbox_id: UUID, error: str
) -> None:
    await conn.execute(
        "UPDATE notification_outbox "
        "   SET status = 'dead', attempt_count = attempt_count + 1, "
        "       last_error = $2, next_attempt_at = NULL "
        " WHERE id = $1",
        outbox_id,
        error[:1000],
    )
