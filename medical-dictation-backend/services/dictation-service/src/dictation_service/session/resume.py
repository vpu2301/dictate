"""Session-resume validation.

Every failure path returns the same opaque ``session_not_found`` so an
attacker can't distinguish "wrong tenant" from "wrong user" from "stale
session" — a deliberate uniform-failure pattern.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:  # heavy / optional deps only at type-check time
    import asyncpg
    from redis.asyncio import Redis

from ..config import settings
from .state import SessionState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    """Result of evaluating a resume request."""

    allowed: bool
    reason: str = ""
    row: "asyncpg.Record | None" = None


async def evaluate_resume(
    conn: "asyncpg.Connection",
    redis: "Redis",
    *,
    session_id: UUID,
    requesting_user: UUID,
    requesting_tenant: UUID,
    live_session_attached: bool,
) -> ResumeOutcome:
    """Run every gate the spec requires; return a single yes/no.

    Reasons are for internal logging only — the WS upgrade handler emits
    `session_not_found` regardless of which gate failed, so callers
    can't infer state.
    """
    if live_session_attached:
        return ResumeOutcome(allowed=False, reason="duplicate_attach")

    row = await conn.fetchrow(
        "SELECT * FROM dictation_sessions WHERE id = $1",
        session_id,
    )
    if row is None:
        return ResumeOutcome(allowed=False, reason="not_in_db")

    if row["tenant_id"] != requesting_tenant:
        return ResumeOutcome(allowed=False, reason="cross_tenant")

    if row["user_id"] != requesting_user:
        return ResumeOutcome(allowed=False, reason="cross_user")

    status = str(row["status"])
    if status not in {"active", "paused", "reconnecting"}:
        return ResumeOutcome(allowed=False, reason=f"bad_state:{status}")

    last_active = row["last_active_at"]
    if isinstance(last_active, datetime):
        age = (datetime.now(last_active.tzinfo) - last_active).total_seconds()
    else:
        age = 0.0
    cutoff = settings.session_idle_abandon_minutes * 60
    if age > cutoff:
        return ResumeOutcome(allowed=False, reason="too_old")

    worker_id = row["worker_id"]
    if worker_id:
        alive = await _worker_alive(redis, worker_id)
        if not alive:
            return ResumeOutcome(allowed=False, reason="worker_dead")

    return ResumeOutcome(allowed=True, reason="ok", row=row)


async def _worker_alive(redis: "Redis", worker_id: str) -> bool:
    ttl_raw: object = await redis.ttl(f"mdx:dict:worker:{worker_id}:hb")
    ttl = int(ttl_raw) if isinstance(ttl_raw, (int, str)) else -2
    return ttl > 0


async def heartbeat_worker(redis: "Redis") -> None:
    """Refresh the worker liveness key. Caller wires the cadence."""
    await redis.set(
        f"mdx:dict:worker:{settings.worker_id}:hb",
        str(int(time.time())),
        ex=int(settings.worker_heartbeat_ttl_s),
    )


@dataclass(frozen=True, slots=True)
class RetransmitDecision:
    accept: bool
    too_large: bool
    duped_seqs: int = 0


def evaluate_retransmit(
    *,
    from_seq: int,
    to_seq: int,
    hwm: int,
) -> RetransmitDecision:
    """Idempotency for retransmit ranges.

    ``hwm`` is the per-session high-water-mark of received seqs. Any
    portion of the requested range that's ≤ hwm is silently deduped.
    Ranges larger than ``MD_RETRANSMIT_MAX_RANGE_FRAMES`` are rejected
    with ``retransmit_too_large`` (sprint-04 spec §6 E9).
    """
    if to_seq <= from_seq:
        return RetransmitDecision(accept=False, too_large=False)
    if (to_seq - from_seq) > settings.retransmit_max_range_frames:
        return RetransmitDecision(accept=False, too_large=True)
    duped = max(0, min(to_seq, hwm + 1) - from_seq)
    return RetransmitDecision(accept=True, too_large=False, duped_seqs=duped)
