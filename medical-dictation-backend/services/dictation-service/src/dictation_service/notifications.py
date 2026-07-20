"""Sprint-12 notification emission for streaming dictation sessions.

Sits beside the WS handler rather than inside `session/finalize.py`.
`finalize_session` is the shared end-of-life routine for every terminal
reason — including `worker_failure` and the abandon timer — and a
session that died is not a session the user wants congratulated on
completing. The handler knows which of those it is; the finalizer does
not.

Fire-and-forget, exactly like report-service's equivalent:
`publish_event` swallows its own failures and the caller does not wait
on fan-out. A dictation must not fail to finalize because the
notification bus is down (ADR-0029).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from notification_events import Category, build_event, publish_event

from .config import settings

logger = logging.getLogger(__name__)


async def emit_dictation_completed(
    redis: object,
    *,
    tenant_id: UUID,
    session_id: UUID,
    user_id: UUID,
    duration_ms: int,
    segments: int,
) -> None:
    """Tell the dictating clinician their session finished processing.

    The recipient hint is the dictating user and nobody else — this is a
    completion receipt for work they started, not a broadcast. It is
    still only a *hint*: notification-service filters it through its own
    tenant's user table, so a wrong id resolves to nothing rather than
    addressing a stranger.

    The payload carries a duration and a segment count. It carries NO
    transcript, not even a leading fragment: the transcript is PHI, and
    this payload is persisted on the notification row and re-read by the
    digest renderer (ADR-0031).
    """
    if not settings.notifications_enabled:
        return

    event = build_event(
        event_id=uuid4(),
        tenant_id=tenant_id,
        category=Category.DICTATION_COMPLETED,
        # The dictating user is both actor and audience. The category's
        # catalog entry sets `exclude_actor=False` for exactly this
        # reason — with the default, this event would notify nobody.
        actor_user_id=user_id,
        resource_type="dictation_session",
        resource_id=session_id,
        occurred_at=datetime.now(UTC),
        recipient_hints=(user_id,),
        payload={"duration_ms": duration_ms, "segments": segments},
    )
    await publish_event(redis, event)
