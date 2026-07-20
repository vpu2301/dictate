"""Sprint-12 notification emission for batch transcription jobs.

A batch job is the strongest case in the system for a notification: it
runs for minutes with nobody watching, and until now its only terminal
signal was a status column the user had to go and poll.

Fire-and-forget, like every other producer: `publish_event` swallows its
own failures and the worker does not await fan-out. A transcription must
not be marked failed because the notification bus is down (ADR-0029).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from notification_events import Category, build_event, publish_event

from .config import settings

logger = logging.getLogger(__name__)


async def emit_transcription_completed(
    redis: object,
    *,
    tenant_id: UUID,
    job_id: UUID,
    requester_sub: UUID,
    duration_ms: int,
    segments: int,
    language: str,
    model: str,
) -> None:
    """Tell the submitter their job produced a transcript."""
    await _emit(
        redis,
        category=Category.TRANSCRIPTION_COMPLETED,
        tenant_id=tenant_id,
        job_id=job_id,
        requester_sub=requester_sub,
        payload={
            "duration_ms": duration_ms,
            "segments": segments,
            "language": language,
            "model": model,
        },
    )


async def emit_transcription_failed(
    redis: object,
    *,
    tenant_id: UUID,
    job_id: UUID,
    requester_sub: UUID,
    error_kind: str,
) -> None:
    """Tell the submitter their job died.

    `error_kind` only — never `error_detail`. The kind is a closed
    vocabulary (corrupt_audio / timeout / gpu_oom); the detail is free
    text built from an exception, and an exception that quotes the audio
    or the partial transcript it choked on would carry PHI into the feed
    (ADR-0031).
    """
    await _emit(
        redis,
        category=Category.TRANSCRIPTION_FAILED,
        tenant_id=tenant_id,
        job_id=job_id,
        requester_sub=requester_sub,
        payload={"error_kind": error_kind},
    )


async def _emit(
    redis: object,
    *,
    category: Category,
    tenant_id: UUID,
    job_id: UUID,
    requester_sub: UUID,
    payload: dict[str, str | int | float | bool | None],
) -> None:
    if not settings.notifications_enabled:
        return

    event = build_event(
        event_id=uuid4(),
        tenant_id=tenant_id,
        category=category,
        # The submitter is both actor and audience: both transcription
        # categories set `exclude_actor=False` in the catalog, because
        # with the default they would resolve to nobody.
        actor_user_id=requester_sub,
        resource_type="transcription_job",
        resource_id=job_id,
        occurred_at=datetime.now(UTC),
        recipient_hints=(requester_sub,),
        payload=payload,
    )
    await publish_event(redis, event)
