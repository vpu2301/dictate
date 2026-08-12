"""Stranded-job reaper.

Every other terminal outcome of a batch job is written by the worker that
owns it: it decodes, it transcribes, it marks the row ``complete`` or
``failed``. That covers every path the worker survives, and none of the
ones it doesn't. Kill the worker mid-inference — SIGKILL, an OOM kill, a
node eviction — and its job stays ``running`` with nobody left to finish
the sentence. Nothing in the system ever revisits that row.

Two stranded shapes, one sweep:

``running`` past the grace window
    The worker died after claiming the job. The queue may well redeliver
    the message and a second worker may produce the transcript — that is
    exactly why the grace window is long and why the update is conditional.

``queued`` past a longer grace window
    The row committed and the enqueue returned, but the message never
    reached a worker: a flushed Redis, a trimmed stream, a consumer group
    recreated underneath it.

Both are worse than cosmetic. ``count_active_jobs`` gates
``per_tenant_concurrent_jobs``, so every stranded row permanently burns a
slot in the tenant's upload budget — enough of them and the tenant cannot
submit at all — and each one shows a clinician a job that will never
resolve.

The reaper lives in asr-service, not in asr-worker, for the obvious
reason: a backstop that runs inside the process being backstopped is not
a backstop. Cross-tenant enumeration goes through the
``asr_tenants_with_stale_jobs`` SECURITY DEFINER function (0077) — the
sanctioned pattern from 0059/0051/0036 — and every row read and write
still happens inside ``tenant_connection``.

The grace windows ARE the safety interlock; asr-worker publishes no
heartbeat to check against. They must stay comfortably above the worst
case the worker allows itself (``max_duration_seconds`` ×
``asr_max_inference_seconds_multiplier``, plus a redelivery or two), or
the reaper will fail jobs that were merely slow. Reaping early is not
catastrophic — the worker's idempotency check sees the terminal row on
redelivery and skips — but it does cost the clinician a transcript that
was on its way.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from asr_models import JobErrorKind
from audit import Severity
from db import tenant_connection

from .. import audit_kinds
from ..config import settings
from . import repository

logger = logging.getLogger(__name__)

# `running` is stranded by a dead worker; `queued` by a lost message. The
# kinds differ because the operator's next question differs.
_KIND_BY_STATUS = {
    "running": str(JobErrorKind.WORKER_LOST),
    "queued": str(JobErrorKind.QUEUE_LOST),
}


async def _tenants_with_stale_jobs(
    state: Any, *, running_grace_seconds: float, queued_grace_seconds: float
) -> list[UUID]:
    """Tenants holding at least one stranded candidate.

    Runs outside ``tenant_connection`` on purpose: the question is
    cross-tenant, which is exactly what the SECURITY DEFINER function
    exists for. Only tenant IDs come back.
    """
    async with state.app_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tenant_id FROM asr_tenants_with_stale_jobs($1, $2)",
            float(running_grace_seconds),
            float(queued_grace_seconds),
        )
    return [r["tenant_id"] for r in rows]


async def reap_tenant(
    state: Any,
    tenant_id: UUID,
    *,
    running_grace_seconds: float,
    queued_grace_seconds: float,
) -> int:
    """Collect one tenant's stranded jobs. Returns how many were reaped."""
    reaped = 0
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        candidates = await repository.list_stale_jobs(
            conn,
            running_grace_seconds=running_grace_seconds,
            queued_grace_seconds=queued_grace_seconds,
            limit=settings.job_reaper_batch_limit,
        )
        for row in candidates:
            kind = _KIND_BY_STATUS.get(row.status)
            if kind is None:  # pragma: no cover — the query filters on status
                continue
            # Conditional on the status we saw: a job that finished between
            # the SELECT and here keeps its own outcome. A stored transcript
            # must never be overwritten by a late "the worker looked dead".
            if not await repository.fail_job(
                conn,
                job_id=row.id,
                error_kind=kind,
                error_detail=(
                    f"reaped after {row.status} beyond the "
                    f"{running_grace_seconds if row.status == 'running' else queued_grace_seconds:.0f}s "
                    "grace window"
                ),
                only_if_status=(row.status,),
            ):
                continue

            reaped += 1
            logger.warning(
                "asr.job_reaped",
                extra={
                    "job_id": str(row.id),
                    "prior_status": row.status,
                    "error_kind": kind,
                },
            )
            try:
                await state.audit_writer.write_event(
                    tenant_id=tenant_id,
                    kind=audit_kinds.TRANSCRIPTION_FAILED,
                    actor_sub=row.requester_sub,
                    target_kind="asr_job",
                    target_id=str(row.id),
                    payload={
                        "error_kind": kind,
                        "prior_status": row.status,
                        "actor": "reaper",
                    },
                    severity=Severity.WARN,
                )
            except Exception as exc:  # noqa: BLE001 — audit must not block the sweep
                logger.warning(
                    "asr.job_reap_audit_failed",
                    extra={"job_id": str(row.id), "error": str(exc)},
                )
    return reaped


async def sweep_once(state: Any) -> int:
    """One full pass across every tenant with candidates."""
    running_grace = float(settings.job_reaper_running_grace_s)
    queued_grace = float(settings.job_reaper_queued_grace_s)
    total = 0
    tenants = await _tenants_with_stale_jobs(
        state,
        running_grace_seconds=running_grace,
        queued_grace_seconds=queued_grace,
    )
    for tenant_id in tenants:
        try:
            total += await reap_tenant(
                state,
                tenant_id,
                running_grace_seconds=running_grace,
                queued_grace_seconds=queued_grace,
            )
        except Exception as exc:  # noqa: BLE001 — one bad tenant must not stop the rest
            logger.warning(
                "asr.job_reap_tenant_failed",
                extra={"tenant_id": str(tenant_id), "error": str(exc)},
            )
    if total:
        logger.info("asr.job_reaper_swept", extra={"reaped": total})
    return total


async def reaper_loop(state: Any, stop: asyncio.Event) -> None:
    """Run :func:`sweep_once` on a timer until ``stop`` is set."""
    interval = float(settings.job_reaper_interval_s)
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
                "asr.job_reaper_failed",
                extra={"error": str(exc), "error_class": type(exc).__name__},
            )
