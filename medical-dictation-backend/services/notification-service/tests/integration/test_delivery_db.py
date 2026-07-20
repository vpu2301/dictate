"""Delivery, retry, dead-lettering and the digest, against real Postgres.

Covers the §6 items for the delivery axis: delivery idempotency, the
retry→sent walk, the retry→dead-letter walk, and digest
idempotency/empty-suppression.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from db import create_pool, tenant_connection
from notification_events import Category, EmailMode, NotificationEvent
from notification_service.adapters.email import FailingProvider, MockProvider
from notification_service.delivery.worker import backoff_delay, deliver_once
from notification_service.domain import repository as repo
from notification_service.domain.materialize import materialize
from notification_service.jobs.digest import run_digest_for_tenant

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_INTEGRATION") != "1",
    reason="set RUN_DB_INTEGRATION=1 to run; needs dev-up + migrate-up",
)

DSN = os.environ.get(
    "DB_APP_ROLE_DSN", "postgresql://app_role:app_role@localhost:5432/medical_dictation"
)
ADMIN_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/medical_dictation"
)

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
CLINICIAN_A = UUID("0c000000-0000-0000-0000-00000000000a")
NURSE_A = UUID("0d000000-0000-0000-0000-00000000000a")


@pytest.fixture
async def pool():
    p = await create_pool(DSN, application_name="notification-delivery-tests")
    try:
        yield p
    finally:
        await p.close()


def _event(**over) -> NotificationEvent:
    base = {
        "event_id": uuid4(),
        "tenant_id": TENANT_A,
        "category": Category.REPORT_SIGNED,
        "actor_user_id": CLINICIAN_A,
        "resource_type": "report",
        "resource_id": uuid4(),
        "occurred_at": datetime.now(UTC),
        "payload": {"report_code": "RPT-DELIV-1", "signature_level": "qualified"},
        "recipient_hints": (NURSE_A,),
    }
    base.update(over)
    return NotificationEvent(**base)


async def _purge(event_id) -> None:
    admin = await create_pool(ADMIN_DSN, application_name="notification-deliv-cleanup")
    try:
        async with admin.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit.notification_dead_letters WHERE tenant_id = $1",
                TENANT_A,
            )
            await conn.execute(
                "DELETE FROM notification_outbox WHERE notification_id IN "
                "(SELECT id FROM notifications WHERE dedupe_key LIKE $1)",
                f"{event_id}%",
            )
            await conn.execute(
                "DELETE FROM notifications WHERE dedupe_key LIKE $1", f"{event_id}%"
            )
            await conn.execute(
                "DELETE FROM notification_digest_progress WHERE tenant_id = $1", TENANT_A
            )
    finally:
        await admin.close()


async def _outbox(pool, notification_id: UUID, channel: str):
    async with tenant_connection(pool, TENANT_A) as conn:
        return await conn.fetchrow(
            "SELECT status, attempt_count, last_error, provider_message_id "
            "  FROM notification_outbox WHERE notification_id = $1 AND channel = $2",
            notification_id,
            channel,
        )


def test_backoff_is_exponential_and_capped() -> None:
    assert backoff_delay(0, base_s=30) == timedelta(seconds=30)
    assert backoff_delay(1, base_s=30) == timedelta(seconds=60)
    assert backoff_delay(2, base_s=30) == timedelta(seconds=120)
    # An uncapped doubling reaches days; a very late notification is
    # worse than a loud failure.
    assert backoff_delay(20, base_s=30) == timedelta(seconds=3600)


async def test_email_is_sent_once_and_is_idempotent(pool) -> None:
    event = _event()
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            result = await materialize(event, conn=conn, app_base_url="http://app")
        nid = result.created[0].notification_id

        provider = MockProvider()
        await deliver_once(
            app_pool=pool, tenant_id=TENANT_A, provider=provider, audit_writer=None
        )
        assert len(provider.sent) == 1
        assert "RPT-DELIV-1" in provider.sent[0].subject

        row = await _outbox(pool, nid, "email")
        assert row["status"] == "sent"

        # Re-driving must NOT produce a second send: the row is no longer
        # `pending`, which is the delivery-idempotency anchor (E3).
        await deliver_once(
            app_pool=pool, tenant_id=TENANT_A, provider=provider, audit_writer=None
        )
        assert len(provider.sent) == 1
    finally:
        await _purge(event.event_id)


async def test_email_carries_no_phi(pool) -> None:
    """The rendered mail holds a code and a link, nothing clinical."""
    event = _event(
        payload={
            "report_code": "RPT-DELIV-2",
            "signature_level": "qualified",
        }
    )
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            await materialize(event, conn=conn, app_base_url="http://app")
        provider = MockProvider()
        await deliver_once(
            app_pool=pool, tenant_id=TENANT_A, provider=provider, audit_writer=None
        )
        body = provider.sent[0].text_body + provider.sent[0].subject
        assert "RPT-DELIV-2" in body
        for token in ("Іваненко", "діабет", "3216549870"):
            assert token not in body
    finally:
        await _purge(event.event_id)


async def test_transient_failure_retries_then_succeeds(pool) -> None:
    """Demo step 8: fail 3x, then succeed; the outbox walks pending→sent."""
    event = _event()
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            result = await materialize(event, conn=conn, app_base_url="http://app")
        nid = result.created[0].notification_id

        provider = FailingProvider(fail_times=3)
        now = datetime.now(UTC)

        for attempt in range(3):
            # Advance the clock past the backoff so the row is due again.
            await deliver_once(
                app_pool=pool,
                tenant_id=TENANT_A,
                provider=provider,
                audit_writer=None,
                now=now + timedelta(hours=attempt + 1),
            )
            row = await _outbox(pool, nid, "email")
            assert row["status"] == "pending", f"attempt {attempt}: not retryable"
            assert row["attempt_count"] == attempt + 1

            async with tenant_connection(pool, TENANT_A) as conn:
                await conn.execute(
                    "UPDATE notification_outbox SET next_attempt_at = now() "
                    " WHERE notification_id = $1 AND channel = 'email'",
                    nid,
                )

        await deliver_once(
            app_pool=pool, tenant_id=TENANT_A, provider=provider, audit_writer=None
        )
        row = await _outbox(pool, nid, "email")
        assert row["status"] == "sent"
        assert len(provider.sent) == 1
    finally:
        await _purge(event.event_id)


async def test_exhausted_retries_dead_letter(pool) -> None:
    """Demo step 8b: past the cap the row goes dead AND leaves evidence."""
    event = _event()
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            result = await materialize(event, conn=conn, app_base_url="http://app")
        nid = result.created[0].notification_id

        provider = FailingProvider(fail_times=99)
        for _ in range(6):
            await deliver_once(
                app_pool=pool, tenant_id=TENANT_A, provider=provider, audit_writer=None
            )
            async with tenant_connection(pool, TENANT_A) as conn:
                await conn.execute(
                    "UPDATE notification_outbox SET next_attempt_at = now() "
                    " WHERE notification_id = $1 AND channel = 'email' "
                    "   AND status = 'pending'",
                    nid,
                )

        row = await _outbox(pool, nid, "email")
        assert row["status"] == "dead"

        # The forensic row is what the DLQ alert fires on (E10).
        admin = await create_pool(ADMIN_DSN, application_name="dl-check")
        try:
            async with admin.acquire() as conn:
                dl = await conn.fetch(
                    "SELECT source, channel, attempt_count FROM "
                    "audit.notification_dead_letters WHERE notification_id = $1",
                    nid,
                )
        finally:
            await admin.close()
        assert len(dl) == 1
        assert dl[0]["source"] == "delivery"
        assert dl[0]["channel"] == "email"
    finally:
        await _purge(event.event_id)


async def test_digest_sends_once_and_suppresses_empty(pool) -> None:
    """E6: one digest per user-day, and never a 'you have 0 things' mail."""
    event = _event(
        category=Category.REPORT_FINALIZED,
        payload={"report_code": "RPT-DIGEST-1"},
    )
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            await repo.upsert_preference(
                conn,
                tenant_id=TENANT_A,
                user_id=NURSE_A,
                category=Category.REPORT_FINALIZED,
                in_app_enabled=True,
                email_mode=EmailMode.DIGEST,
            )
            await materialize(event, conn=conn, app_base_url="http://app")

        provider = MockProvider()
        # 12:00 UTC is past an 08:00 local digest hour.
        at = datetime.now(UTC).replace(hour=12, minute=0)

        sent = await run_digest_for_tenant(
            app_pool=pool, tenant_id=TENANT_A, provider=provider, now=at
        )
        assert sent == 1
        assert len(provider.sent) == 1
        assert "RPT-DIGEST-1" in provider.sent[0].text_body

        # Second run the same day must claim nothing.
        sent_again = await run_digest_for_tenant(
            app_pool=pool, tenant_id=TENANT_A, provider=provider, now=at
        )
        assert sent_again == 0
        assert len(provider.sent) == 1, "a user must not get two digests in a day"
    finally:
        async with tenant_connection(pool, TENANT_A) as conn:
            await conn.execute(
                "DELETE FROM notification_preferences WHERE user_id=$1 AND category=$2",
                NURSE_A,
                str(Category.REPORT_FINALIZED),
            )
        await _purge(event.event_id)


async def test_digest_not_due_before_local_hour(pool) -> None:
    event = _event(
        category=Category.REPORT_FINALIZED, payload={"report_code": "RPT-DIGEST-2"}
    )
    try:
        async with tenant_connection(pool, TENANT_A) as conn:
            await repo.upsert_preference(
                conn,
                tenant_id=TENANT_A,
                user_id=NURSE_A,
                category=Category.REPORT_FINALIZED,
                in_app_enabled=True,
                email_mode=EmailMode.DIGEST,
            )
            await materialize(event, conn=conn, app_base_url="http://app")

        provider = MockProvider()
        # 02:00 UTC == 04:00/05:00 Kyiv, before the 08:00 default.
        at = datetime.now(UTC).replace(hour=2, minute=0)
        sent = await run_digest_for_tenant(
            app_pool=pool, tenant_id=TENANT_A, provider=provider, now=at
        )
        assert sent == 0
        assert provider.sent == []
    finally:
        async with tenant_connection(pool, TENANT_A) as conn:
            await conn.execute(
                "DELETE FROM notification_preferences WHERE user_id=$1 AND category=$2",
                NURSE_A,
                str(Category.REPORT_FINALIZED),
            )
        await _purge(event.event_id)


async def test_mock_provider_refuses_production() -> None:
    """A mock that silently eats mail in prod is worse than no mail."""
    with pytest.raises(RuntimeError, match="never be used in production"):
        MockProvider(is_production=True)
