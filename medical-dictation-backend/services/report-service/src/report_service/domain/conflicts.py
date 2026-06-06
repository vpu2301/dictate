"""Conflict exceptions raised by domain layer + routed to HTTP 409/422."""

from __future__ import annotations


class OptimisticLockMismatch(Exception):
    def __init__(self, *, current_version: int, expected_version: int) -> None:
        self.current_version = current_version
        self.expected_version = expected_version
        super().__init__(
            f"expected version {expected_version}, current is {current_version}"
        )


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: int, detail: str) -> None:
        self.retry_after_seconds = retry_after_seconds
        self.detail = detail
        super().__init__(detail)


class ReportNotFound(Exception):
    pass
