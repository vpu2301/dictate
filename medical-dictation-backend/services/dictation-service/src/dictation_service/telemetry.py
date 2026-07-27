"""Periodic worker-state gauges (sprint-14 deployment).

Counters and histograms are emitted at their event sites. Gauges describe a
*standing* condition — how loaded this worker is, how much device memory the
two models hold, whether it can take a conversation session — so they are
sampled on a timer instead.

Why this exists at all: sprint 04 declared ``mdx_dictation_active_sessions``
and ``mdx_dictation_model_loaded`` in ``metrics.py`` but never emitted them,
so the sprint-04 dashboard and the ``DictationWorkerSaturated`` alert have
been querying series that never existed. Conversation mode makes that
untenable — the whole point of weighted capacity is that you can watch it.

Device memory is deliberately device-agnostic:

* **CUDA host** — ``torch.cuda.mem_get_info()``: whole-device used/total,
  which is what a "VRAM at 90%" alert must watch (a co-tenant process
  filling the card is exactly the failure being guarded against).
* **CPU host** — process RSS against total system memory. Not VRAM, and
  labelled ``kind="rss"`` so no dashboard can quietly conflate the two.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
from typing import TYPE_CHECKING, Any

from . import metrics
from .config import settings

if TYPE_CHECKING:
    from .main_deps import ServiceState

logger = logging.getLogger(__name__)

# How often the gauges are refreshed. Prometheus scrapes every 15 s
# (infra/prometheus/prometheus.yml), so 10 s guarantees a fresh sample per
# scrape without adding measurable load.
SAMPLE_INTERVAL_S = 10.0


def device_memory() -> tuple[int, int, str]:
    """Return ``(used_bytes, total_bytes, kind)`` for the inference device.

    ``kind`` is ``"vram"`` on a CUDA host and ``"rss"`` on a CPU host. A
    ``total`` of 0 means the reading is unavailable; callers skip the ratio.
    """
    try:
        import torch

        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            return int(total - free), int(total), "vram"
    except Exception:  # noqa: BLE001 — telemetry must never break the worker
        pass
    return _process_rss_bytes(), _system_memory_bytes(), "rss"


def _process_rss_bytes() -> int:
    if platform.system() == "Linux":
        try:
            with open(f"/proc/{os.getpid()}/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            return 0
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes; every other platform reports KB.
        return int(peak) if platform.system() == "Darwin" else int(peak) * 1024
    except Exception:  # noqa: BLE001
        return 0


def _system_memory_bytes() -> int:
    try:
        return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        return 0


def sample_gauges(state: Any) -> None:
    """Set every worker-state gauge once. Safe to call from a timer."""
    worker = {"worker_id": settings.worker_id}

    # ── capacity ────────────────────────────────────────────────────
    sessions = state.session_manager.all()
    by_mode: dict[str, int] = {"dictation": 0, "conversation": 0}
    for ctx in sessions:
        by_mode[ctx.mode] = by_mode.get(ctx.mode, 0) + 1
    for mode, count in by_mode.items():
        metrics.active_sessions.set(count, {**worker, "mode": mode})

    used = state.session_manager.total_weight
    metrics.capacity_weight_used.set(used, worker)
    metrics.capacity_weight_max.set(settings.per_worker_max_sessions, worker)

    # ── model residency / conversation readiness ────────────────────
    metrics.model_loaded.set(1 if state.engine.is_loaded else 0, {**worker, "model": "whisper"})
    diar = state.diarization_engine
    metrics.model_loaded.set(1 if diar.loaded else 0, {**worker, "model": "diarizer"})
    metrics.conversation_ready.set(1 if diar.ready_for_conversation else 0, worker)

    # ── device memory ───────────────────────────────────────────────
    used_bytes, total_bytes, kind = device_memory()
    attrs = {**worker, "kind": kind}
    metrics.device_memory_bytes.set(used_bytes, attrs)
    if total_bytes > 0:
        metrics.device_memory_total_bytes.set(total_bytes, attrs)
        metrics.device_memory_utilization.set(used_bytes / total_bytes, attrs)


async def gauge_loop(state: ServiceState, stop: asyncio.Event) -> None:
    """Refresh worker-state gauges until ``stop`` is set."""
    while not stop.is_set():
        try:
            sample_gauges(state)
        except Exception as exc:  # noqa: BLE001 — telemetry never kills the worker
            logger.warning(
                "telemetry.gauge_sample_failed",
                extra={"error": str(exc), "error_class": type(exc).__name__},
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=SAMPLE_INTERVAL_S)
            return
        except TimeoutError:
            continue
