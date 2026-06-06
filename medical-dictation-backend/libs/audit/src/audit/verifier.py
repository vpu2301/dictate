"""``AuditVerifier`` — replay a tenant's audit chain and surface tampering.

Algorithm:

1. Acquire a connection from the audit_reader pool.
2. ``SET LOCAL app.tenant_id`` so RLS restricts the walk to one tenant.
3. ``SELECT seq, payload_jcs, prev_hash, payload_hash FROM audit.events
   WHERE tenant_id = $1 AND seq BETWEEN $2 AND $3 ORDER BY seq`` — pulls
   the entire requested range in one query (the verifier is meant to be
   batch / nightly; for very long chains, the caller chunks via from_seq /
   to_seq).
4. Walk the rows. At each row, the *expected* ``payload_hash`` is
   ``sha256(running || jcs(payload_jcs))``. The first row's expected
   ``prev_hash`` is the genesis (32 zero bytes); each subsequent row's
   expected ``prev_hash`` is the previous row's stored ``payload_hash``.
5. On any mismatch — wrong prev_hash, wrong payload_hash, or a gap in seq
   numbers — return a ``VerificationReport`` describing the first
   divergence and stop. The chain is "ok" only when every row in the
   range verifies.

Divergence reasons:

- ``gap``  — seq numbers are not consecutive (e.g. 1, 2, 4 means seq=3 is
             missing and we report at seq=4 with reason gap).
- ``prev_hash_mismatch`` — the row's stored ``prev_hash`` doesn't match
             the previous row's stored ``payload_hash``.
- ``payload_hash_mismatch`` — the recomputed hash differs from stored
             ``payload_hash`` (the payload_jcs has been tampered with).

The verifier itself never modifies state.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg

from .canonical import canonicalize
from .writer import GENESIS_PREV_HASH

logger = logging.getLogger(__name__)


class DivergenceReason(StrEnum):
    GAP = "gap"
    PREV_HASH_MISMATCH = "prev_hash_mismatch"
    PAYLOAD_HASH_MISMATCH = "payload_hash_mismatch"


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Outcome of a chain walk.

    ``ok=True``  → ``last_seq`` and ``last_hash`` describe the last
                   successfully verified event in the range (or
                   ``None``/``None`` when the range is empty).
    ``ok=False`` → ``first_divergence_seq`` is the seq of the failing row,
                   ``divergence_reason`` says why, and ``expected_hash`` /
                   ``actual_hash`` (when applicable) carry the diagnosis.
    """

    ok: bool
    tenant_id: UUID
    from_seq: int
    to_seq: int | None
    events_checked: int
    last_seq: int | None = None
    last_hash: bytes | None = None
    first_divergence_seq: int | None = None
    divergence_reason: DivergenceReason | None = None
    expected_hash: bytes | None = None
    actual_hash: bytes | None = None


class AuditVerifier:
    """Walk a tenant's audit chain and assert hash continuity.

    Parameters
    ----------
    pool
        An asyncpg pool authenticated as the Postgres ``audit_reader`` (or
        ``audit_writer``) role — anything with SELECT on ``audit.events``.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def verify_chain(
        self,
        tenant_id: UUID,
        *,
        from_seq: int = 1,
        to_seq: int | None = None,
    ) -> VerificationReport:
        if from_seq < 1:
            raise ValueError("from_seq must be ≥ 1")
        if to_seq is not None and to_seq < from_seq:
            raise ValueError("to_seq must be ≥ from_seq")

        async with self._pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
                )

                if to_seq is None:
                    rows = await conn.fetch(
                        """
                        SELECT seq, payload_jcs::text AS payload_jcs,
                               prev_hash, payload_hash
                        FROM audit.events
                        WHERE tenant_id = $1 AND seq >= $2
                        ORDER BY seq
                        """,
                        tenant_id,
                        from_seq,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT seq, payload_jcs::text AS payload_jcs,
                               prev_hash, payload_hash
                        FROM audit.events
                        WHERE tenant_id = $1 AND seq BETWEEN $2 AND $3
                        ORDER BY seq
                        """,
                        tenant_id,
                        from_seq,
                        to_seq,
                    )

        # Empty range = vacuously ok.
        if not rows:
            return VerificationReport(
                ok=True,
                tenant_id=tenant_id,
                from_seq=from_seq,
                to_seq=to_seq,
                events_checked=0,
            )

        # If from_seq > 1 the running hash starts at the *previous* row's
        # payload_hash. For from_seq == 1, it starts at the genesis seed.
        if from_seq == 1:
            running = GENESIS_PREV_HASH
        else:
            running = await self._fetch_prev_hash_seed(tenant_id, from_seq)

        events_checked = 0
        expected_seq = from_seq

        for row in rows:
            seq = int(row["seq"])

            # 1. Sequence gap → stop and report on this seq.
            if seq != expected_seq:
                return VerificationReport(
                    ok=False,
                    tenant_id=tenant_id,
                    from_seq=from_seq,
                    to_seq=to_seq,
                    events_checked=events_checked,
                    first_divergence_seq=expected_seq,
                    divergence_reason=DivergenceReason.GAP,
                )

            stored_prev = bytes(row["prev_hash"]) if row["prev_hash"] is not None else GENESIS_PREV_HASH

            # 2. prev_hash continuity → ensures the row's stated lineage
            #    matches the chain we've walked so far.
            if stored_prev != running:
                return VerificationReport(
                    ok=False,
                    tenant_id=tenant_id,
                    from_seq=from_seq,
                    to_seq=to_seq,
                    events_checked=events_checked,
                    first_divergence_seq=seq,
                    divergence_reason=DivergenceReason.PREV_HASH_MISMATCH,
                    expected_hash=running,
                    actual_hash=stored_prev,
                )

            # 3. Recompute payload_hash and compare → catches in-place
            #    tampering of payload_jcs OR of payload_hash itself.
            payload_dict: Any = json.loads(row["payload_jcs"])
            jcs_bytes = canonicalize(payload_dict)
            expected_payload_hash = hashlib.sha256(running + jcs_bytes).digest()
            stored_payload_hash = bytes(row["payload_hash"])

            if expected_payload_hash != stored_payload_hash:
                return VerificationReport(
                    ok=False,
                    tenant_id=tenant_id,
                    from_seq=from_seq,
                    to_seq=to_seq,
                    events_checked=events_checked,
                    first_divergence_seq=seq,
                    divergence_reason=DivergenceReason.PAYLOAD_HASH_MISMATCH,
                    expected_hash=expected_payload_hash,
                    actual_hash=stored_payload_hash,
                )

            running = stored_payload_hash
            events_checked += 1
            expected_seq += 1

        return VerificationReport(
            ok=True,
            tenant_id=tenant_id,
            from_seq=from_seq,
            to_seq=to_seq,
            events_checked=events_checked,
            last_seq=expected_seq - 1,
            last_hash=running,
        )

    async def _fetch_prev_hash_seed(self, tenant_id: UUID, from_seq: int) -> bytes:
        """When verifying a sub-range, seed the running hash from the row
        immediately before ``from_seq``.

        Wrapped in an explicit transaction so the ``set_config(..., true)``
        setting is visible to the subsequent SELECT (transaction-local).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
                )
                row = await conn.fetchrow(
                    """
                    SELECT payload_hash FROM audit.events
                    WHERE tenant_id = $1 AND seq = $2
                    """,
                    tenant_id,
                    from_seq - 1,
                )
        if row is None:
            # The caller asked for a range starting past the chain head, or
            # the seed row is missing (which is itself a divergence — we let
            # the gap check on the next iteration surface it). Treat as
            # genesis for now so the walker's first comparison surfaces the
            # break.
            return GENESIS_PREV_HASH
        return bytes(row["payload_hash"])
