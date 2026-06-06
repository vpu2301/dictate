"""OpenTelemetry metric instruments for the streaming surface.

Names match sprint-04 spec §9 verbatim — the Grafana dashboard and
alerts reference them. Keep stable.
"""

from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter("mdx.dictation")

# Gauges
active_sessions = _meter.create_gauge(
    "mdx_dictation_active_sessions",
    description="Live sessions per worker",
    unit="1",
)
model_loaded = _meter.create_gauge(
    "mdx_dictation_model_loaded",
    description="1 if Whisper model is loaded on this worker",
    unit="1",
)

# Histograms
partial_latency_ms = _meter.create_histogram(
    "mdx_dictation_partial_latency_ms",
    description="From VAD-speech start to PARTIAL emit",
    unit="ms",
)
final_latency_ms = _meter.create_histogram(
    "mdx_dictation_final_latency_ms",
    description="From VAD silence boundary to FINAL emit",
    unit="ms",
)
window_inference_ms = _meter.create_histogram(
    "mdx_dictation_window_inference_ms",
    description="Wall-clock for one Whisper transcribe_window call",
    unit="ms",
)
rtf = _meter.create_histogram(
    "mdx_dictation_rtf",
    description="Realtime factor = audio_seconds / wall_seconds",
    unit="1",
)
opus_decode_us = _meter.create_histogram(
    "mdx_dictation_opus_decode_us",
    description="Opus → PCM decode time per frame",
    unit="us",
)
bandwidth_bps = _meter.create_histogram(
    "mdx_dictation_bandwidth_bps",
    description="Per-session inbound bytes-per-second",
    unit="bps",
)

# Counters
session_drops = _meter.create_counter(
    "mdx_dictation_session_drops_total",
    description="Sessions dropped by reason",
    unit="1",
)
reconnects = _meter.create_counter(
    "mdx_dictation_reconnects_total",
    description="Sessions resumed after a network drop",
    unit="1",
)
audio_decode_errors = _meter.create_counter(
    "mdx_dictation_audio_decode_errors_total",
    description="Per-frame Opus decode failures",
    unit="1",
)
ws_upgrade_rejections = _meter.create_counter(
    "mdx_dictation_ws_upgrade_rejections_total",
    description="Rejected WS upgrades by reason",
    unit="1",
)
