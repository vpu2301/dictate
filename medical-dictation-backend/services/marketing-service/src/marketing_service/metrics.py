"""OTel instruments. Named so a dashboard reads as a funnel, top to bottom."""

from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter("marketing-service")

demo_requests = _meter.create_counter(
    "mdx_demo_requests_total",
    description="Demo requests accepted from the public form",
)
demo_requests_rejected = _meter.create_counter(
    "mdx_demo_requests_rejected_total",
    description="Demo requests refused (rate limit, invalid address)",
)
bookings_detected = _meter.create_counter(
    "mdx_demo_bookings_detected_total",
    description="Calendar invitations recognised as demo bookings",
)
bookings_unmatched = _meter.create_counter(
    "mdx_demo_bookings_unmatched_total",
    description="Bookings with no demo request to attach them to",
)
mail_attempts = _meter.create_counter(
    "mdx_demo_mail_attempts_total",
    description="Send attempts, by mail kind",
)
mail_failures = _meter.create_counter(
    "mdx_demo_mail_failures_total",
    description="Failed send attempts, by kind and permanence",
)
mail_dead_lettered = _meter.create_counter(
    "mdx_demo_mail_dead_lettered_total",
    description=(
        "Mails abandoned after the attempt budget. Every one of these is a "
        "prospect who asked to see the product and heard nothing — alert on it."
    ),
)
booking_watch_failures = _meter.create_counter(
    "mdx_demo_booking_watch_failures_total",
    description="IMAP poll cycles that raised. Sustained non-zero = bookings are being missed",
)
