"""libs/audit — tamper-evident hash-chained audit event log.

Public API:

- :class:`AuditWriter` — the single sanctioned writer. Construct with an
  asyncpg pool authenticated as the Postgres ``audit_writer`` role.
- :class:`Severity` — info / warn / sec / error. Drives alerting + retention.
- :func:`canonicalize` — RFC 8785 JSON canonicalization.
- :data:`GENESIS_PREV_HASH` — 32 zero bytes, the chain's seed.

See ADR-0008 (Day 10) for design rationale.
"""

from __future__ import annotations

from .canonical import canonicalize, canonicalize_str
from .exceptions import (
    AuditError,
    CanonicalizationError,
    ChainWriteError,
    TenantMismatchError,
)
from .types import AuditEventReceipt, Severity
from .verifier import AuditVerifier, DivergenceReason, VerificationReport
from .writer import GENESIS_PREV_HASH, AuditWriter

__all__ = [
    "AuditError",
    "AuditEventReceipt",
    "AuditVerifier",
    "AuditWriter",
    "CanonicalizationError",
    "ChainWriteError",
    "DivergenceReason",
    "GENESIS_PREV_HASH",
    "Severity",
    "TenantMismatchError",
    "VerificationReport",
    "canonicalize",
    "canonicalize_str",
]
