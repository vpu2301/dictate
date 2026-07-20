"""Sprint-12 notification emission for report lifecycle transitions.

Sits at the router layer, not in `domain/report_lifecycle.py`. The state
machine is a pure function of (conn, report_id, status) and has neither
the acting user nor the report's authors in scope; emitting from there
would mean re-reading the row and threading Redis through the domain
layer, inverting `routers → domain`.

Everything here is fire-and-forget: `publish_event` swallows its own
failures, and the callers do not await fan-out. Finalizing a report must
not fail because the notification bus is down (ADR-0029).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from notification_events import Category, build_event, publish_event

from .config import settings

logger = logging.getLogger(__name__)


async def emit_report_event(
    redis: object,
    *,
    category: Category,
    tenant_id: UUID,
    report_id: UUID,
    report_code: str,
    actor_user_id: UUID | None,
    primary_author_id: UUID | None = None,
    co_author_ids: tuple[UUID, ...] = (),
    version_id: UUID | None = None,
    extra_payload: dict[str, str | int | float | bool | None] | None = None,
) -> None:
    """Publish one report lifecycle fact.

    `recipient_hints` carries the author set because report-service owns
    the `reports` row and already knows it. notification-service filters
    those ids through its own tenant's user table, so a stale or wrong
    hint can never address someone in another tenant.
    """
    if not settings.notifications_enabled:
        return

    hints: list[UUID] = []
    if primary_author_id is not None:
        hints.append(primary_author_id)
    hints.extend(co_author_ids)

    # `report_code` ONLY — never the report title. A clinician-authored
    # title routinely contains a diagnosis, and this payload reaches an
    # email subject line (ADR-0031).
    payload: dict[str, str | int | float | bool | None] = {"report_code": report_code}
    if extra_payload:
        payload.update(extra_payload)

    event = build_event(
        event_id=uuid4(),
        tenant_id=tenant_id,
        category=category,
        actor_user_id=actor_user_id,
        resource_type="report",
        resource_id=report_id,
        resource_version_id=version_id,
        occurred_at=datetime.now(UTC),
        recipient_hints=tuple(hints),
        payload=payload,
    )
    await publish_event(redis, event)
