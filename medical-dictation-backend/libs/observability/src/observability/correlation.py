"""Inject OTel ``trace_id`` / ``span_id`` into stdlib log records.

Use as a logging filter; placed before the JSON formatter so the trace
context appears as top-level fields in every log line. Falls back to
empty strings when no span is active.
"""

from __future__ import annotations

import logging

try:  # pragma: no cover — optional at import; required at runtime
    from opentelemetry import trace
except ImportError:  # pragma: no cover
    trace = None  # type: ignore[assignment]


class CorrelationIdFilter(logging.Filter):
    """Add ``trace_id`` and ``span_id`` to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if trace is None:
            record.trace_id = ""
            record.span_id = ""
            return True
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx is None or not ctx.is_valid:
            record.trace_id = ""
            record.span_id = ""
            return True
        record.trace_id = format(ctx.trace_id, "032x")
        record.span_id = format(ctx.span_id, "016x")
        return True
