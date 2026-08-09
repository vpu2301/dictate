"""The sprint-16 scheduler pattern — one runner, hosted per service.

A single scheduler *process* would have to import three services (jobs
live in report-, autocomplete- and core-service), which the import-linter
contracts forbid — so the "one pattern" is this shared runner, hosted
in-process by each service behind its ``MDX_BACKGROUND_JOBS`` flag (the
autocomplete precedent), with every job also exposed as a
``python -m``/``scripts/jobs`` CLI for external cron (the sprint-08/11
precedent). ADR-0041 records the choice.

The runner owns the loop mechanics and the Prometheus-side contract:

- ``mdx_scheduler_job_runs_total{job, outcome}``
- ``mdx_scheduler_job_duration_seconds{job}``

The per-run **audit row** is the job's own concern (jobs hold the audit
writer and tenant context; this leaf lib must not import libs/audit) —
pass ``on_complete`` to write ``scheduler.job.completed/failed``.

Every job MUST be idempotent: the first iteration fires immediately at
startup (self-healing after downtime), and a crashed run is simply
retried next interval.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry import metrics

logger = logging.getLogger(__name__)

_meter = metrics.get_meter("mdx.scheduler")
_runs_total = _meter.create_counter(
    "mdx_scheduler_job_runs_total",
    description="Scheduled job runs by job name and outcome",
    unit="1",
)
_duration = _meter.create_histogram(
    "mdx_scheduler_job_duration_seconds",
    description="Scheduled job run duration",
    unit="s",
)


async def run_job_once(
    *,
    job_name: str,
    fn: Callable[[], Awaitable[Any]],
    on_complete: Callable[[str, Any, float], Awaitable[None]] | None = None,
) -> Any:
    """Run one iteration: metrics + logs + optional audit callback.

    Returns the job result on success; swallows (and logs) job errors —
    the loop must survive any single failure. ``on_complete`` receives
    ``(outcome, result_or_error_str, duration_seconds)`` and is itself
    best-effort.
    """
    started = time.monotonic()
    try:
        result = await fn()
        outcome = "ok"
        detail: Any = result
    except Exception as exc:  # noqa: BLE001 — the loop must survive
        outcome = "error"
        detail = f"{type(exc).__name__}: {exc}"
        logger.exception("scheduler.job_failed", extra={"job": job_name})
        result = None
    duration = time.monotonic() - started
    _runs_total.add(1, {"job": job_name, "outcome": outcome})
    _duration.record(duration, {"job": job_name})
    logger.info(
        "scheduler.job_finished",
        extra={"job": job_name, "outcome": outcome, "duration_s": round(duration, 3)},
    )
    if on_complete is not None:
        try:
            await on_complete(outcome, detail, duration)
        except Exception:  # noqa: BLE001 — audit is best-effort here
            logger.exception("scheduler.on_complete_failed", extra={"job": job_name})
    return result


async def run_periodic(
    *,
    job_name: str,
    interval_seconds: float,
    fn: Callable[[], Awaitable[Any]],
    on_complete: Callable[[str, Any, float], Awaitable[None]] | None = None,
) -> None:
    """Loop ``run_job_once`` forever; first iteration fires immediately."""
    while True:
        await run_job_once(job_name=job_name, fn=fn, on_complete=on_complete)
        await asyncio.sleep(interval_seconds)
