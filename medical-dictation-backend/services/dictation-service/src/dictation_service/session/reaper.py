"""Stale-session reaper.

The abandon timer that retires an idle session (``_abandon_after_idle`` in
``ws/handler.py``) is an ``asyncio`` task inside the worker that owns the
session. That works for every graceful path and for none of the ungraceful
ones: kill the worker and its timers die with it, leaving every session it
held parked in ``active`` / ``paused`` / ``reconnecting`` **forever**. Those
rows are not cosmetic — ``count_active_for_tenant`` gates
``per_tenant_max_active_sessions``, so each stranded row permanently burns a
slot in the tenant's capacity budget, and each one keeps showing up in the
clinician's "still recording" list.

The reaper is the out-of-process backstop. Its single safety interlock is
the worker heartbeat in Redis: a session is collected **only** when the
worker named on the row has stopped heart-beating. A session paused for an
hour on a healthy worker is a candidate every sweep and is never collected.

Cross-tenant enumeration goes through the ``dictation_tenants_with_stale_sessions``
SECURITY DEFINER function (0059) — the sanctioned pattern from 0051/0036 —
and every row read and write still happens inside ``tenant_connection``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from audit import Severity
from db import tenant_connection

from .. import audit_kinds
from ..config import settings
from ..domain import repository
from .resume import worker_alive

logger = logging.getLogger(__name__)


async def _tenants_with_stale_sessions(state: Any, grace_seconds: float) -> list[UUID]:
    """Tenants holding at least one stale candidate.

    Runs outside ``tenant_connection`` on purpose: the question is
    cross-tenant, which is exactly what the SECURITY DEFINER function
    exists for. Only tenant IDs come back.
    """
    async with state.app_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tenant_id FROM dictation_tenants_with_stale_sessions($1)",
            float(grace_seconds),
        )
    return [r["tenant_id"] for r in rows]


async def reap_tenant(state: Any, tenant_id: UUID, *, grace_seconds: float) -> int:
    """Collect one tenant's stranded sessions. Returns how many were reaped."""
    reaped = 0
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        candidates = await repository.list_stale_sessions(
            conn,
            grace_seconds=grace_seconds,
            limit=settings.session_reaper_batch_limit,
        )
        for row in candidates:
            session_id = row["id"]

            # Held in *this* process: its own timers own it, and reaping it
            # underneath a live SessionContext would desync ctx.state from
            # the DB.
            if state.session_manager.get(session_id) is not None:
                continue

            owner = row["worker_id"]
            # A blank worker_id predates the column being populated; there is
            # no liveness signal to wait on, so the grace window is the only
            # gate and it has already elapsed.
            if owner and await worker_alive(state.redis, owner):
                continue

            if not await repository.abandon_if_still_stale(
                conn, session_id=session_id, expected_status=row["status"]
            ):
                continue  # raced back to life; leave it alone

            reaped += 1
            logger.warning(
                "session.reaped",
                extra={
                    "session_id": str(session_id),
                    "prior_status": row["status"],
                    "owner_worker": owner or "",
                },
            )
            try:
                await state.audit_writer.write_event(
                    tenant_id=tenant_id,
                    kind=audit_kinds.SESSION_ABANDONED,
                    actor_sub=row["user_id"],
                    target_kind="dictation_session",
                    target_id=str(session_id),
                    payload={
                        "reason": "reaped_dead_worker",
                        "prior_status": row["status"],
                        "owner_worker": owner or "",
                        "encounter_id": str(row["encounter_id"])
                        if row["encounter_id"]
                        else None,
                    },
                    severity=Severity.WARN,
                )
            except Exception as exc:  # noqa: BLE001 — audit must not block the sweep
                logger.warning(
                    "session.reap_audit_failed",
                    extra={"session_id": str(session_id), "error": str(exc)},
                )
    return reaped


async def sweep_once(state: Any) -> int:
    """One full pass across every tenant with candidates."""
    grace = float(settings.session_reaper_grace_s)
    total = 0
    for tenant_id in await _tenants_with_stale_sessions(state, grace):
        try:
            total += await reap_tenant(state, tenant_id, grace_seconds=grace)
        except Exception as exc:  # noqa: BLE001 — one bad tenant must not stop the rest
            logger.warning(
                "session.reap_tenant_failed",
                extra={"tenant_id": str(tenant_id), "error": str(exc)},
            )
    if total:
        logger.info("session.reaper_swept", extra={"reaped": total})
    return total


async def reaper_loop(state: Any, stop: asyncio.Event) -> None:
    """Run :func:`sweep_once` on a timer until ``stop`` is set."""
    interval = float(settings.session_reaper_interval_s)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            await sweep_once(state)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session.reaper_failed",
                extra={"error": str(exc), "error_class": type(exc).__name__},
            )
