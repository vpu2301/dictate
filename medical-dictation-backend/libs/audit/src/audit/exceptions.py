"""Distinct exception classes for audit writer failure modes."""

from __future__ import annotations


class AuditError(Exception):
    """Base class for libs/audit errors."""


class CanonicalizationError(AuditError):
    """The payload could not be JCS-canonicalized.

    Causes: non-JSON-serialisable types in the payload (e.g. raw datetime,
    UUID — callers must pre-convert), or values that JCS rejects (NaN,
    Infinity).
    """


class ChainWriteError(AuditError):
    """Failed to commit an audit event to the chain after retries."""


class TenantMismatchError(AuditError):
    """Caller asked to write to tenant A but the connection is scoped to B."""
