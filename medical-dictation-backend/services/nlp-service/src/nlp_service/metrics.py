"""OTel metric instruments for the NLP pipeline.

Names match sprint-05 spec §10 verbatim — the Grafana dashboard +
alerts reference them. Stable contract.
"""

from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter("mdx.nlp")

# Per-stage latency is recorded inside the orchestrator; redeclared here
# so importers can find every instrument in one place.
stage_duration_ms = _meter.create_histogram(
    "mdx_nlp_request_duration_ms",
    description="Per-stage latency",
    unit="ms",
)

punctuation_fallback = _meter.create_counter(
    "mdx_nlp_punctuation_fallback_total",
    description="Per-call punctuation fallbacks (model timeout / failure)",
    unit="1",
)

voice_commands_detected = _meter.create_counter(
    "mdx_nlp_voice_commands_detected_total",
    description="Voice commands detected, by intent",
    unit="1",
)

voice_commands_undone = _meter.create_counter(
    "mdx_nlp_voice_commands_undone_total",
    description="Voice commands undone (frontend-emitted via audit forwarder)",
    unit="1",
)

voice_command_undo_rate = _meter.create_gauge(
    "mdx_nlp_voice_command_undo_rate",
    description="Derived: undone / executed over a rolling window",
    unit="1",
)

cache_hit_ratio = _meter.create_gauge(
    "mdx_nlp_cache_hit_ratio",
    description="Idempotence-cache hit ratio",
    unit="1",
)

idempotence_violations = _meter.create_counter(
    "mdx_nlp_idempotence_violations_total",
    description="Identical inputs produced different outputs — bug.",
    unit="1",
)

oversized_input = _meter.create_counter(
    "mdx_nlp_oversized_input_total",
    description="413s from request-size validator",
    unit="1",
)

rate_limit_total = _meter.create_counter(
    "mdx_nlp_rate_limit_total",
    description="429s by scope=tenant|ip",
    unit="1",
)
