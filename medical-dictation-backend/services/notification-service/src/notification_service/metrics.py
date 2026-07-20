"""OTel instruments for notification-service.

Every metric named by `infra/prometheus/rules/sprint-12-alerts.yml` is
created HERE, and `tests/unit/test_alert_rules.py` asserts the two sets
match in both directions. Sprint 10's post-mortem was precisely this
drift: dashboards and alerts referring to metrics nothing exported, so
the alerts could never fire and nobody noticed until the feature had
been silently broken for weeks.
"""

from __future__ import annotations

from typing import Final

from opentelemetry import metrics

_meter = metrics.get_meter("mdx.notification")

# ── ingest ──────────────────────────────────────────────────────────
events_consumed = _meter.create_counter(
    "mdx_notification_events_consumed_total",
    description="Envelopes successfully consumed from the event stream",
    unit="1",
)
events_rejected = _meter.create_counter(
    "mdx_notification_events_rejected_total",
    description="Envelopes that could never be processed (malformed)",
    unit="1",
)
notifications_created = _meter.create_counter(
    "mdx_notification_created_total",
    description="Notification rows materialised",
    unit="1",
)
coalesced = _meter.create_counter(
    "mdx_notification_coalesced_total",
    description="Notifications folded into a coalescing row by the storm cap (E1)",
    unit="1",
)

# ── delivery ────────────────────────────────────────────────────────
delivery_attempts = _meter.create_counter(
    "mdx_notification_delivery_attempts_total",
    description="Outbox delivery attempts, per channel",
    unit="1",
)
delivery_failures = _meter.create_counter(
    "mdx_notification_delivery_failures_total",
    description="Delivery attempts that failed and will be retried",
    unit="1",
)
dead_letters = _meter.create_counter(
    "mdx_notification_dead_letters_total",
    description="Deliveries abandoned after exhausting retries (E10)",
    unit="1",
)
suppressed = _meter.create_counter(
    "mdx_notification_suppressed_total",
    description="Channels deliberately not dispatched, by reason (E8)",
    unit="1",
)

# ── fan-out ─────────────────────────────────────────────────────────
fanout_latency_ms = _meter.create_histogram(
    "mdx_notification_fanout_latency_ms",
    description="Materialisation → WebSocket frame delivered, milliseconds",
    unit="ms",
)

# ── observable gauges ───────────────────────────────────────────────
# These are registered by main_deps with callbacks, because their value
# is read from live state rather than accumulated.
STREAM_PENDING_GAUGE: Final = "mdx_notification_stream_pending"
DIGEST_LAST_RUN_GAUGE: Final = "mdx_notification_digest_last_run_unix_ts"
CONNECTED_SOCKETS_GAUGE: Final = "mdx_notification_connected_sockets"


def register_gauges(*, stream_pending_cb, digest_last_run_cb, connected_sockets_cb) -> None:
    """Wire the observable gauges. Called once at startup."""
    _meter.create_observable_gauge(
        STREAM_PENDING_GAUGE,
        callbacks=[stream_pending_cb],
        description="Entries pending in the notification consumer group (E10)",
        unit="1",
    )
    _meter.create_observable_gauge(
        DIGEST_LAST_RUN_GAUGE,
        callbacks=[digest_last_run_cb],
        description="Unix timestamp of the last successful digest run",
        unit="s",
    )
    _meter.create_observable_gauge(
        CONNECTED_SOCKETS_GAUGE,
        callbacks=[connected_sockets_cb],
        description="WebSocket clients connected to THIS worker",
        unit="1",
    )
