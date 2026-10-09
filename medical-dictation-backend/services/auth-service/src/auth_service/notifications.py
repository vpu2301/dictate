"""S21 notification emission — the MFA reminder.

auth-service's first producer, and it exists because auth-service is the
only service that knows an account has no second factor.

The reminder has two halves and this module is the smaller one:

  · STANDING — the `mfa_reminders` row, returned by ``GET /auth/me`` and
    rendered as a banner the user cannot dismiss. It is what makes the
    reminder last "until they set it".
  · ARRIVING — this event: the bell, and an email, because a user with no
    second factor is very often a user who signs in rarely, and a banner
    only reminds someone who is already looking at the app.

Fire-and-forget, like every other producer in the estate (ADR-0029):
``publish_event`` swallows its own failures and the caller does not wait
on fan-out. A reminder must not fail to be RECORDED because the
notification bus is down — the row is the durable half, and the endpoint
returns 201 either way.

PHI: none is reachable from here. The payload carries the requester's
ROLE (a closed vocabulary), never their name, and a count. See the
allow-list in notification-service's `domain/render.py`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from notification_events import Category, build_event, publish_event

from .config import settings

logger = logging.getLogger(__name__)


async def emit_mfa_reminder(
    redis: object | None,
    *,
    tenant_id: UUID,
    subject_sub: UUID,
    actor_sub: UUID,
    actor_role: str,
    reminder_count: int,
) -> None:
    """Tell one user that an access review asked them to enrol MFA.

    The recipient hint is the subject and nobody else. Fanning this out to
    a tenant's admins — or to anyone but the account holder — would be
    publishing which accounts are the weak ones, which is the opposite of
    what the review is for.
    """
    if not settings.notifications_enabled or redis is None:
        return

    event = build_event(
        event_id=uuid4(),
        tenant_id=tenant_id,
        category=Category.SECURITY_MFA_REMINDER,
        # The actor is the reviewer; the audience is the subject. The
        # catalog entry sets `exclude_actor=False`, which changes nothing
        # here (the endpoint refuses a self-reminder) but keeps the
        # routing honest if that ever changes.
        actor_user_id=actor_sub,
        resource_type="user",
        resource_id=subject_sub,
        occurred_at=datetime.now(UTC),
        recipient_hints=(subject_sub,),
        payload={"requested_by_role": actor_role, "reminder_count": reminder_count},
    )
    try:
        await publish_event(redis, event)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — the reminder row is already committed
        logger.warning("mfa_reminder.publish_failed", extra={"error": str(exc)})
