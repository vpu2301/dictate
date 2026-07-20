"""Daily digest — one email per user per day, or none at all.

Idempotency differs deliberately from sprint-10's rollup. That job does
check-then-act (SELECT marker → work → INSERT marker), which lets two
runners both pass the check and double-count. For a digest the same race
sends one user two emails (E6), so the claim here is taken FIRST and a
unique-violation means "another worker owns this user-day".

Timezone correctness matters as much as idempotency: the digest fires at
the user's LOCAL `digest_hour`, computed through zoneinfo, so a DST
transition does not shift everyone's morning summary by an hour (E9).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import asyncpg

from audit import AuditWriter
from audit import Severity as AuditSeverity
from db import create_pool, tenant_connection
from notification_events import Category

from .. import audit_kinds
from ..adapters.email import EmailProvider, OutboundEmail, build_provider
from ..adapters.templates import render_email
from ..config import settings
from ..domain import repository as repo
from ..domain.catalog import digest_eligible_categories, spec_for
from ..domain.preferences import UserSettings, resolve_timezone

logger = logging.getLogger(__name__)

# Read by the `mdx_notification_digest_last_run_unix_ts` gauge; the
# DigestStale alert fires on its age.
_last_success_unix: float = 0.0


def last_success_unix() -> float:
    return _last_success_unix


def is_due(settings_row: UserSettings, *, now: datetime) -> bool:
    """Has this user's local digest hour arrived today?

    Compared in local wall-clock time. A user at digest_hour=8 in
    Europe/Kyiv gets their mail at 08:00 Kyiv all year, not at a fixed
    UTC offset that drifts across the DST boundary.
    """
    local = now.astimezone(resolve_timezone(settings_row.timezone))
    return local.hour >= settings_row.digest_hour


async def claim_user_day(
    conn: asyncpg.Connection, *, tenant_id: UUID, user_id: UUID, day: date
) -> bool:
    """Atomically claim (day, tenant, user). False = someone else has it.

    INSERT-first, not SELECT-then-INSERT: the primary key does the
    mutual exclusion, so two workers racing cannot both proceed.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO notification_digest_progress (digest_date, tenant_id, user_id)
        VALUES ($1::date, $2, $3)
        ON CONFLICT (digest_date, tenant_id, user_id) DO NOTHING
        RETURNING digest_date
        """,
        day,
        tenant_id,
        user_id,
    )
    return row is not None


async def finish_user_day(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    user_id: UUID,
    day: date,
    included: int,
) -> None:
    await conn.execute(
        "UPDATE notification_digest_progress "
        "   SET finished_at = now(), notifications_included = $4 "
        " WHERE digest_date = $1::date AND tenant_id = $2 AND user_id = $3",
        day,
        tenant_id,
        user_id,
        included,
    )


async def pending_digest_rows(
    conn: asyncpg.Connection, *, user_id: UUID, since: datetime
) -> list[asyncpg.Record]:
    """Digest-eligible notifications whose email stood down for the digest."""
    categories = [str(c) for c in sorted(digest_eligible_categories(), key=str)]
    return await conn.fetch(
        """
        SELECT n.id, n.title, n.created_at
          FROM notifications n
          JOIN notification_outbox o
            ON o.notification_id = n.id AND o.channel = 'email'
         WHERE n.recipient_user_id = $1
           AND n.category = ANY($2::text[])
           AND n.created_at >= $3
           AND o.status = 'suppressed'
           AND o.suppressed_reason = 'digest_deferred'
         ORDER BY n.created_at
        """,
        user_id,
        categories,
        since,
    )


async def run_digest_for_tenant(
    *,
    app_pool: asyncpg.Pool,
    tenant_id: UUID,
    provider: EmailProvider,
    audit_writer: AuditWriter | None = None,
    now: datetime | None = None,
    day: date | None = None,
) -> int:
    """Returns the number of digests actually sent."""
    at = now or datetime.now(UTC)
    digest_day = day or at.date()
    since = at - timedelta(days=1)
    sent = 0

    async with tenant_connection(app_pool, tenant_id) as conn:
        candidates = await conn.fetch(
            """
            SELECT DISTINCT n.recipient_user_id AS user_id
              FROM notifications n
              JOIN notification_outbox o
                ON o.notification_id = n.id AND o.channel = 'email'
             WHERE o.status = 'suppressed'
               AND o.suppressed_reason = 'digest_deferred'
               AND n.created_at >= $1
            """,
            since,
        )

        for row in candidates:
            user_id: UUID = row["user_id"]
            user_settings = await repo.load_settings(conn, user_id=user_id)
            if not is_due(user_settings, now=at):
                continue

            if not await claim_user_day(
                conn, tenant_id=tenant_id, user_id=user_id, day=digest_day
            ):
                continue  # another worker owns this user-day

            items = await pending_digest_rows(conn, user_id=user_id, since=since)
            if not items:
                # Empty-digest suppression: never email "you have 0
                # things". The claim stays, so we do not re-check all day.
                await finish_user_day(
                    conn, tenant_id=tenant_id, user_id=user_id, day=digest_day, included=0
                )
                continue

            address = await repo.user_email(conn, user_id)
            if not address:
                await finish_user_day(
                    conn, tenant_id=tenant_id, user_id=user_id, day=digest_day, included=0
                )
                continue

            # The lines are the ALREADY-RENDERED PHI-free titles, so the
            # digest cannot surface anything the individual notifications
            # did not.
            lines = [r["title"] for r in items]
            rendered = render_email(
                Category.SYSTEM_DIGEST,
                template_stem=spec_for(Category.SYSTEM_DIGEST).email_template,
                fields={"count": str(len(items)), "period": "день"},
                deep_link=settings.app_base_url.rstrip("/") + "/notifications",
                items=lines,
            )

            try:
                await provider.send(
                    OutboundEmail(
                        to_address=address,
                        subject=rendered.subject,
                        text_body=rendered.text_body,
                        html_body=rendered.html_body,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # Leave finished_at NULL: the claim row now reads as a
                # crashed run, which the runbook treats as investigate
                # rather than silently retry (and re-send).
                logger.warning(
                    "digest.send_failed",
                    extra={"user_id": str(user_id), "error": str(exc)},
                )
                continue

            # Mark the individual rows as folded into the digest so a
            # second day's run cannot include them again.
            await conn.execute(
                "UPDATE notification_outbox SET status = 'sent', "
                "       suppressed_reason = 'digest_sent' "
                " WHERE channel = 'email' AND notification_id = ANY($1::uuid[])",
                [r["id"] for r in items],
            )
            await finish_user_day(
                conn,
                tenant_id=tenant_id,
                user_id=user_id,
                day=digest_day,
                included=len(items),
            )
            sent += 1

            if audit_writer is not None:
                await audit_writer.write_event(
                    tenant_id=tenant_id,
                    kind=audit_kinds.DIGEST_SENT,
                    actor_sub=None,
                    actor_role="system",
                    target_kind="notification",
                    target_id=str(user_id),
                    payload={"digest_date": digest_day.isoformat(), "included": len(items)},
                    severity=AuditSeverity.INFO,
                )

    return sent


async def run_all(*, now: datetime | None = None) -> int:  # pragma: no cover
    global _last_success_unix
    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name="notification-service/digest",
        min_size=1,
        max_size=4,
    )
    audit_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name="notification-service/digest-audit",
        min_size=1,
        max_size=2,
    )
    provider = build_provider(
        kind=settings.email_provider,
        is_production=settings.is_production,
        host=settings.smtp_host,
        port=settings.smtp_port,
        from_address=settings.email_from,
        from_name=settings.email_from_name,
        use_tls=settings.smtp_use_tls,
        username=settings.smtp_username,
        password=settings.smtp_password,
    )
    total = 0
    try:
        async with app_pool.acquire() as conn:
            # SECURITY DEFINER (migration 0051). A plain read of `tenants`
            # is RLS-filtered to zero rows on an unscoped connection, so
            # this job reported success while doing nothing at all.
            tenants = await conn.fetch(
                "SELECT tenant_id AS id FROM notification_active_tenant_ids()"
            )
        writer = AuditWriter(audit_pool)
        for t in tenants:
            try:
                total += await run_digest_for_tenant(
                    app_pool=app_pool,
                    tenant_id=t["id"],
                    provider=provider,
                    audit_writer=writer,
                    now=now,
                )
            except Exception:  # noqa: BLE001
                logger.exception("digest.tenant_failed", extra={"tenant_id": str(t["id"])})
        _last_success_unix = (now or datetime.now(UTC)).timestamp()
    finally:
        await provider.aclose()
        await app_pool.close()
        await audit_pool.close()
    return total


def main() -> int:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sent = asyncio.run(run_all())
    print(f"digest completed; sent={sent}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
