"""Materialisation against a real Postgres, under real RLS.

Covers the §6 verification items that only a live database can prove:
tenant isolation, materialisation idempotency, recipient-rule routing,
and the suppressed-outbox audit trail.

Gated: RUN_DB_INTEGRATION=1, plus `make dev-up && make migrate-up`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from db import create_pool, tenant_connection
from notification_events import Category, EmailMode, NotificationEvent
from notification_service.domain import repository as repo
from notification_service.domain.materialize import materialize

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs dev-up + migrate-up",
)

DSN = os.environ.get(
    "DB_APP_ROLE_DSN", "postgresql://app_role:app_role@localhost:5432/medical_dictation"
)
# Cleanup only — see _cleanup().
ADMIN_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/medical_dictation"
)

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")
ADMIN_A = UUID("0a000000-0000-0000-0000-00000000000a")
CLINICIAN_A = UUID("0c000000-0000-0000-0000-00000000000a")
NURSE_A = UUID("0d000000-0000-0000-0000-00000000000a")
CLINICIAN_B = UUID("0c000000-0000-0000-0000-00000000000b")


@pytest.fixture
async def pool():
    p = await create_pool(DSN, application_name="notification-tests")
    try:
        yield p
    finally:
        await p.close()


def _event(**over: object) -> NotificationEvent:
    base: dict[str, object] = {
        "event_id": uuid4(),
        "tenant_id": TENANT_A,
        "category": Category.REPORT_FINALIZED,
        "actor_user_id": CLINICIAN_A,
        "resource_type": "report",
        "resource_id": uuid4(),
        "occurred_at": datetime.now(UTC),
        "payload": {"report_code": "RPT-TEST-1"},
    }
    base.update(over)
    return NotificationEvent(**base)  # type: ignore[arg-type]


async def _cleanup(_pool, tenant_id: UUID, dedupe_prefix: str) -> None:
    """Tear down as the superuser.

    `app_role` is deliberately denied DELETE on notifications and the
    outbox (the policies are `USING (false)` — delivery history is
    evidence). Test cleanup therefore cannot reuse the app pool; that it
    fails to is the schema working correctly.
    """
    admin = await create_pool(ADMIN_DSN, application_name="notification-tests-cleanup")
    try:
        async with admin.acquire() as conn:
            await conn.execute(
                "DELETE FROM notification_outbox WHERE notification_id IN "
                "(SELECT id FROM notifications WHERE tenant_id=$1 AND dedupe_key LIKE $2)",
                tenant_id,
                f"{dedupe_prefix}%",
            )
            await conn.execute(
                "DELETE FROM notifications WHERE tenant_id=$1 AND dedupe_key LIKE $2",
                tenant_id,
                f"{dedupe_prefix}%",
            )
    finally:
        await admin.close()


async def test_materialize_is_idempotent(pool) -> None:
    """The same event twice → exactly one row (the at-least-once contract)."""
    event = _event(recipient_hints=(NURSE_A,))
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            first = await materialize(event, conn=conn, app_base_url="http://x")
        assert len(first.created) == 1

        async with tenant_connection(pool, TENANT_A) as conn:
            second = await materialize(event, conn=conn, app_base_url="http://x")
        assert second.created == []
        assert second.duplicates == 1

        async with tenant_connection(pool, TENANT_A) as conn:
            rows = await conn.fetch(
                "SELECT id FROM notifications WHERE dedupe_key = $1",
                event.dedupe_key(NURSE_A),
            )
        assert len(rows) == 1
    finally:
        await _cleanup(pool, TENANT_A, str(event.event_id))


async def test_actor_is_excluded_from_own_fanout(pool) -> None:
    event = _event(recipient_hints=(CLINICIAN_A, NURSE_A))
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            result = await materialize(event, conn=conn, app_base_url="http://x")
        recipients = {c.recipient_user_id for c in result.created}
        assert recipients == {NURSE_A}, "the actor must not be told about their own action"
    finally:
        await _cleanup(pool, TENANT_A, str(event.event_id))


async def test_cross_tenant_hint_materialises_nothing(pool) -> None:
    """A producer naming a foreign user must not reach them (§6 isolation)."""
    event = _event(recipient_hints=(CLINICIAN_B,))
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            result = await materialize(event, conn=conn, app_base_url="http://x")
        assert result.created == []
        assert result.recipients_considered == 0

        # And nothing landed in tenant B either.
        async with tenant_connection(pool, TENANT_B) as conn:
            rows = await conn.fetch(
                "SELECT id FROM notifications WHERE dedupe_key = $1",
                event.dedupe_key(CLINICIAN_B),
            )
        assert rows == []
    finally:
        await _cleanup(pool, TENANT_A, str(event.event_id))


async def test_notification_is_invisible_to_the_other_tenant(pool) -> None:
    event = _event(recipient_hints=(NURSE_A,))
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            await materialize(event, conn=conn, app_base_url="http://x")

        async with tenant_connection(pool, TENANT_B) as conn:
            leaked = await conn.fetch(
                "SELECT id FROM notifications WHERE dedupe_key = $1",
                event.dedupe_key(NURSE_A),
            )
        assert leaked == [], "RLS must hide tenant A's notification from tenant B"
    finally:
        await _cleanup(pool, TENANT_A, str(event.event_id))


async def test_chain_failure_routes_to_admins_only(pool) -> None:
    """Demo step 6: the admin hears about it, the clinician does not."""
    event = _event(
        category=Category.REPORT_CHAIN_FAILURE,
        actor_user_id=None,
        recipient_hints=(),
        payload={"report_code": "RPT-TEST-2", "check_name": "chain_reconciler"},
    )
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            result = await materialize(event, conn=conn, app_base_url="http://x")
        recipients = {c.recipient_user_id for c in result.created}
        assert ADMIN_A in recipients
        assert CLINICIAN_A not in recipients
        assert NURSE_A not in recipients
    finally:
        await _cleanup(pool, TENANT_A, str(event.event_id))


async def test_suppressed_email_is_recorded_not_skipped(pool) -> None:
    """E8: turning email off leaves auditable proof, not silence."""
    event = _event(
        category=Category.REPORT_SIGNED,
        recipient_hints=(NURSE_A,),
        actor_user_id=CLINICIAN_A,
        payload={"report_code": "RPT-TEST-3"},
    )
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            await repo.upsert_preference(
                conn,
                tenant_id=TENANT_A,
                user_id=NURSE_A,
                category=Category.REPORT_SIGNED,
                in_app_enabled=True,
                email_mode=EmailMode.OFF,
            )
            result = await materialize(event, conn=conn, app_base_url="http://x")
            assert len(result.created) == 1

            rows = await conn.fetch(
                "SELECT channel, status, suppressed_reason FROM notification_outbox "
                " WHERE notification_id = $1 ORDER BY channel",
                result.created[0].notification_id,
            )

        by_channel = {r["channel"]: r for r in rows}
        assert by_channel["email"]["status"] == "suppressed"
        assert by_channel["email"]["suppressed_reason"] == "preference"
        assert by_channel["in_app"]["status"] == "pending"
    finally:
        # Tenant-scoped: an unscoped app_role connection resolves
        # `app.tenant_id` to '' and the policy's uuid cast rejects it —
        # RLS refusing an unscoped write, exactly as intended.
        async with tenant_connection(pool, TENANT_A) as conn:
            await conn.execute(
                "DELETE FROM notification_preferences WHERE user_id=$1 AND category=$2",
                NURSE_A,
                str(Category.REPORT_SIGNED),
            )
        await _cleanup(pool, TENANT_A, str(event.event_id))


async def test_feed_and_unread_count_agree(pool) -> None:
    event = _event(recipient_hints=(NURSE_A,))
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            before = await repo.unread_count(conn, user_id=NURSE_A)
            result = await materialize(event, conn=conn, app_base_url="http://x")
            after = await repo.unread_count(conn, user_id=NURSE_A)
        assert after == before + 1
        new_id = result.created[0].notification_id

        async with tenant_connection(pool, TENANT_A) as conn:
            rows = await repo.list_feed(
                conn,
                user_id=NURSE_A,
                limit=10,
                before_created_at=None,
                before_id=None,
                unread_only=True,
            )
        # The row the badge counted must be the row the feed shows —
        # the two must not be able to disagree.
        assert new_id in {r["id"] for r in rows}
    finally:
        await _cleanup(pool, TENANT_A, str(event.event_id))


async def test_mark_read_is_idempotent(pool) -> None:
    event = _event(recipient_hints=(NURSE_A,))
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            result = await materialize(event, conn=conn, app_base_url="http://x")
            nid = result.created[0].notification_id

            assert await repo.mark_read(conn, user_id=NURSE_A, notification_id=nid)
            first_row = await conn.fetchrow(
                "SELECT read_at FROM notifications WHERE id = $1", nid
            )

            # Second mark must succeed and must NOT move the timestamp.
            assert await repo.mark_read(conn, user_id=NURSE_A, notification_id=nid)
            second_row = await conn.fetchrow(
                "SELECT read_at FROM notifications WHERE id = $1", nid
            )

        assert first_row["read_at"] == second_row["read_at"]
    finally:
        await _cleanup(pool, TENANT_A, str(event.event_id))


async def test_other_user_cannot_mark_your_notification_read(pool) -> None:
    event = _event(recipient_hints=(NURSE_A,))
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            result = await materialize(event, conn=conn, app_base_url="http://x")
            nid = result.created[0].notification_id

            # Same tenant, wrong recipient — the recipient predicate, not
            # RLS, is what stops this.
            assert not await repo.mark_read(
                conn, user_id=CLINICIAN_A, notification_id=nid
            )
            row = await conn.fetchrow(
                "SELECT read_at FROM notifications WHERE id = $1", nid
            )
        assert row["read_at"] is None
    finally:
        await _cleanup(pool, TENANT_A, str(event.event_id))
