"""libs/observability — single entry point + RFC 9457 + PII filter."""

from .bootstrap import bootstrap
from .correlation import CorrelationIdFilter
from .logging import setup_logging
from .metrics import setup_metrics
from .pii_filter import PIISafeFilter, scrub, scrub_event_dict
from .problem_details import (
    PROBLEM_CONTENT_TYPE,
    ProblemDetails,
    register_exception_handlers,
)
from .tracing import setup_tracing

__all__ = [
    "CorrelationIdFilter",
    "PIISafeFilter",
    "PROBLEM_CONTENT_TYPE",
    "ProblemDetails",
    "bootstrap",
    "register_exception_handlers",
    "scrub",
    "scrub_event_dict",
    "setup_logging",
    "setup_metrics",
    "setup_tracing",
]
