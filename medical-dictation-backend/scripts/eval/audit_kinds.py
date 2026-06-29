"""Audit/event kinds emitted by the sprint-07 WER eval pipeline.

The eval pipeline is a non-tenant, system-level CI job, so these are
surfaced as structured log events (and Slack alerts) rather than
hash-chained ``audit.events`` rows, which require a tenant context. The
catalogue lives here so the strings are typo-proof and documented in
``docs/audit/event-kinds.md``.
"""

from __future__ import annotations

from typing import Final

RUN_STARTED: Final = "eval.run.started"  # info — a WER eval run began
RUN_COMPLETED: Final = "eval.run.completed"  # info — run finished, scores recorded
RUN_REGRESSED: Final = "eval.run.regressed"  # warn — run breached a baseline threshold

EVAL_AUDIT_KINDS: frozenset[str] = frozenset(
    {RUN_STARTED, RUN_COMPLETED, RUN_REGRESSED}
)
