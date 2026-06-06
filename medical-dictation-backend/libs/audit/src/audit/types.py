"""Typed surfaces for libs/audit: severity enum, event-kind catalogue marker, receipt."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Severity(StrEnum):
    """Audit event severity. Drives alerting + retention policy.

    - ``info``  — routine business events (logout, read, list).
    - ``warn``  — suspicious but not necessarily malicious (rate-limit hit).
    - ``sec``   — security-relevant (login failed, refresh replayed, MFA
                  disabled, RLS denial). Triggers SIEM alerting.
    - ``error`` — system error worth recording (audit writer retry exhausted,
                  chain divergence detected by nightly verify).
    """

    INFO = "info"
    WARN = "warn"
    SEC = "sec"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AuditEventReceipt:
    """Returned by :meth:`AuditWriter.write_event` on success.

    Useful when the caller wants to log the assigned sequence number, or
    when an admin-facing endpoint wants to surface the chain receipt as
    proof of recording.
    """

    tenant_id: UUID
    seq: int
    payload_hash: bytes
