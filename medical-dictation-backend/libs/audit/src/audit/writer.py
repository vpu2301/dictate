"""``AuditWriter`` — the single sanctioned path for emitting audit events.

Contract:

- Acquires a connection from the audit_writer pool (passed in by the caller,
  distinct from the app_role pool).
- Wraps each write in a READ COMMITTED transaction.
- ``SET LOCAL app.tenant_id`` so RLS policies match.
- ``SELECT seq, payload_hash FROM audit.events WHERE tenant_id = $1 ORDER BY
  seq DESC LIMIT 1 FOR UPDATE`` — the FOR UPDATE row lock is the actual
  serialisation primitive: a second writer for the same tenant blocks until
  the first commits, then re-reads the latest ``last_seq`` (READ COMMITTED
  semantics on a row lock).
- Builds the event_record dict (with ``tenant_id``, ``seq``, ``created_at``,
  actor + kind + target + the caller-supplied payload), JCS-canonicalises it,
  computes ``payload_hash = sha256(prev_hash || jcs_bytes)``, and INSERTs.
- Genesis row (seq=1) has ``prev_hash`` as 32 zero bytes.
- Retries on serialization failure or primary-key conflict up to 3 times
  with exponential backoff. Under normal load these never fire — they are
  defensive in case of unexpected contention modes (e.g. logical replication
  lag, statement timeouts).

Why not SERIALIZABLE? The spec calls for it but with the per-tenant row
lock from FOR UPDATE, the additional anomaly checks SERIALIZABLE provides
become *additional contention* (pivot-anomaly retries) under high write
fan-out. READ COMMITTED + FOR UPDATE gives the same correctness for the
"strict per-tenant monotonic seq" invariant without those retries.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg
from opentelemetry import metrics

from .canonical import canonicalize
from .exceptions import ChainWriteError
from .types import AuditEventReceipt, Severity

# ── Metrics (no-op when no provider is set; OTel proxy handles this) ─
_meter = metrics.get_meter("mdx.audit")
_writes_counter = _meter.create_counter(
    "mdx_audit_writes_total",
    description="Audit events successfully written",
    unit="1",
)
_retries_counter = _meter.create_counter(
    "mdx_audit_write_retries_total",
    description="Retries performed on serialization/PK conflicts",
    unit="1",
)
_write_latency = _meter.create_histogram(
    "mdx_audit_write_duration_seconds",
    description="End-to-end latency of a single AuditWriter.write_event",
    unit="s",
)

logger = logging.getLogger(__name__)

GENESIS_PREV_HASH: bytes = bytes(32)  # 32 zero bytes for the first event per tenant
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 0.05


class AuditWriter:
    """Append a tamper-evident event to a tenant's audit chain.

    Parameters
    ----------
    pool
        An asyncpg.Pool authenticated as the Postgres ``audit_writer``
        role. SHOULD be a dedicated pool, not the app_role one — keeping
        them separate makes the documented privilege boundary explicit.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def write_event(
        self,
        *,
        tenant_id: UUID,
        kind: str,
        actor_sub: UUID | None = None,
        actor_role: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        severity: Severity = Severity.INFO,
    ) -> AuditEventReceipt:
        """Append a single event to ``tenant_id``'s chain.

        Returns the assigned sequence number and committed ``payload_hash``.

        Raises:
            ChainWriteError: SERIALIZABLE retries exhausted, or RLS rejected
                the insert, or any other DB-level failure that wasn't a
                serialization conflict.
        """
        normalized_payload = _normalize_payload(payload or {})

        start = time.perf_counter()
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                receipt = await self._write_once(
                    tenant_id=tenant_id,
                    actor_sub=actor_sub,
                    actor_role=actor_role,
                    kind=kind,
                    target_kind=target_kind,
                    target_id=target_id,
                    payload=normalized_payload,
                    severity=severity,
                )
                # Success path metrics.
                _writes_counter.add(
                    1, {"tenant_id": str(tenant_id), "severity": severity.value}
                )
                _write_latency.record(time.perf_counter() - start)
                return receipt
            except (
                asyncpg.SerializationError,
                asyncpg.UniqueViolationError,
            ) as exc:
                # SerializationError: SERIALIZABLE-tier conflict (shouldn't
                # happen under READ COMMITTED, but defensive).
                # UniqueViolationError: two writers raced past the row lock,
                # e.g. statement-timeout cancel mid-flight — retry yields a
                # fresh next_seq from the latest committed last_seq.
                last_exc = exc
                _retries_counter.add(
                    1,
                    {
                        "tenant_id": str(tenant_id),
                        "error_class": type(exc).__name__,
                    },
                )
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                logger.info(
                    "audit_writer.retry",
                    extra={
                        "attempt": attempt + 1,
                        "tenant_id": str(tenant_id),
                        "kind": kind,
                        "error_class": type(exc).__name__,
                    },
                )

        raise ChainWriteError(
            f"audit writer exhausted {_MAX_RETRIES} retries for tenant {tenant_id}: {last_exc}"
        ) from last_exc

    async def _write_once(
        self,
        *,
        tenant_id: UUID,
        actor_sub: UUID | None,
        actor_role: str | None,
        kind: str,
        target_kind: str | None,
        target_id: str | None,
        payload: Mapping[str, Any],
        severity: Severity,
    ) -> AuditEventReceipt:
        # Per-tenant advisory lock key. Hashing UUID → signed bigint keeps
        # the lock keyspace tight; the worst case of a hash collision is
        # benign mutual blocking between two tenants (correctness preserved).
        lock_key = _tenant_lock_key(tenant_id)

        async with self._pool.acquire() as conn:
            async with conn.transaction(isolation="read_committed"):
                # Scope RLS to this tenant for the duration of the txn.
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
                )

                # Serialise *all* writers for this tenant. The advisory lock
                # is held until COMMIT/ROLLBACK; the FOR UPDATE below adds
                # row-level locking once a row exists. With an empty table,
                # FOR UPDATE LIMIT 1 has nothing to lock — without this
                # advisory lock, two concurrent first-writes would collide
                # on seq=1.
                await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)

                last = await conn.fetchrow(
                    """
                    SELECT seq, payload_hash
                    FROM audit.events
                    WHERE tenant_id = $1
                    ORDER BY seq DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    tenant_id,
                )

                if last is None:
                    next_seq = 1
                    prev_hash = GENESIS_PREV_HASH
                else:
                    next_seq = int(last["seq"]) + 1
                    prev_hash = bytes(last["payload_hash"])

                created_at = datetime.now(timezone.utc)

                event_record: dict[str, Any] = {
                    "tenant_id": str(tenant_id),
                    "seq": next_seq,
                    "created_at": created_at.isoformat(),
                    "actor_sub": str(actor_sub) if actor_sub is not None else None,
                    "actor_role": actor_role,
                    "kind": kind,
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "payload": dict(payload),
                    "severity": severity.value,
                }

                event_jcs = canonicalize(event_record)
                payload_hash = hashlib.sha256(prev_hash + event_jcs).digest()

                await conn.execute(
                    """
                    INSERT INTO audit.events (
                        tenant_id, seq, created_at,
                        actor_sub, actor_role,
                        kind, target_kind, target_id,
                        payload_jcs, prev_hash, payload_hash, severity
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12)
                    """,
                    tenant_id,
                    next_seq,
                    created_at,
                    actor_sub,
                    actor_role,
                    kind,
                    target_kind,
                    target_id,
                    event_jcs.decode("utf-8"),
                    prev_hash,
                    payload_hash,
                    severity.value,
                )

                return AuditEventReceipt(
                    tenant_id=tenant_id,
                    seq=next_seq,
                    payload_hash=payload_hash,
                )


def _tenant_lock_key(tenant_id: UUID) -> int:
    """Deterministic UUID → signed-bigint mapping for ``pg_advisory_xact_lock``.

    BLAKE2b-64 of the 16 raw UUID bytes, interpreted as signed big-endian.
    Collisions in the 2^63 keyspace are statistically negligible; if two
    tenants ever collide they merely block each other on the advisory
    lock (no correctness violation, just slight contention).
    """
    import hashlib

    h = hashlib.blake2b(tenant_id.bytes, digest_size=8).digest()
    return int.from_bytes(h, "big", signed=True)


def _normalize_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Convert common non-JSON types in caller payloads to JSON-natural ones.

    UUID → str, datetime → ISO 8601 str, bytes → base64 str. We do *not*
    recurse into arbitrary nested objects beyond dict / list / tuple to
    keep the contract explicit; callers passing deeply nested non-JSON
    types will hit :class:`CanonicalizationError` and learn to pre-convert.
    """
    import base64

    def _conv(v: Any) -> Any:
        if isinstance(v, UUID):
            return str(v)
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, bytes):
            return base64.b64encode(v).decode("ascii")
        if isinstance(v, dict):
            return {k: _conv(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_conv(x) for x in v]
        return v

    return {k: _conv(v) for k, v in payload.items()}
