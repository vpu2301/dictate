"""Per-tenant Key-Encryption-Key (KEK) repository.

A tenant KEK is the only key that ever wraps DEKs for that tenant. It is
stored in the ``tenant_keks`` table, wrapped under the master KEK, with
exactly one row per tenant.

Lifecycle:

1. First encrypt for a new tenant: ``get_or_create`` allocates a fresh
   32-byte KEK with :func:`os.urandom`, wraps it under the master, and
   INSERTs a row via the ``crypto_writer`` Postgres role.
2. Subsequent calls fetch the wrapped KEK, unwrap it via the master
   provider, and cache the plaintext for a short TTL.
3. Rotation (sprint 17): ``rotate(tenant_id)`` evicts the cache and
   ``wraps`` a new KEK in-place; old DEKs are still decryptable until a
   re-wrap migration runs (KMS swap path, ADR-0011).

Why a separate ``crypto_writer`` role? The least-privilege model in
sprint 02 split DB roles by responsibility. ``app_role`` can SELECT
``tenant_keks`` (filtered by RLS to the caller's tenant), but it
deliberately can't write — that's the crypto_writer role.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from uuid import UUID

import asyncpg

from .master import MASTER_KEY_SIZE_BYTES, MasterKeyProvider

logger = logging.getLogger(__name__)

# Plaintext-KEK cache TTL. Short on purpose — the cache exists to absorb
# burst load, not to be a long-lived key store. 60 s is the spec maximum.
_CACHE_TTL_SECONDS: float = 60.0


@dataclass(slots=True)
class _CachedKek:
    plaintext: bytes
    master_key_id: str
    cached_at: float


class TenantKekRepository:
    """Per-tenant KEK fetch / create with bounded plaintext caching.

    Construct with a ``crypto_writer``-backed asyncpg pool and a master
    key provider. Reuse the instance across requests.
    """

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        master_key_provider: MasterKeyProvider,
        cache_ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> None:
        self._pool = pool
        self._master = master_key_provider
        self._cache: dict[UUID, _CachedKek] = {}
        self._lock = asyncio.Lock()
        self._cache_ttl = cache_ttl_seconds

    def master_key_id_for(self, tenant_id: UUID) -> str:
        """Return the master_key_id under which the tenant's KEK is wrapped.

        Reads the cache only — callers MUST have called ``get_or_create``
        first, which populates the cache. Used by :class:`Envelope` when
        building :class:`EnvelopeBlob` metadata.
        """
        entry = self._cache.get(tenant_id)
        if entry is None:
            raise KeyError(
                f"master_key_id requested for {tenant_id} before "
                "get_or_create was called; refusing to invent a value."
            )
        return entry.master_key_id

    def evict(self, tenant_id: UUID) -> None:
        """Drop the cached plaintext KEK for ``tenant_id``.

        Called on rotation, or by adversarial-leak handlers that want to
        force a re-fetch on the next request.
        """
        cached = self._cache.pop(tenant_id, None)
        if cached is not None:
            # Best-effort zero: overwriting the dataclass field is the
            # extent of what CPython allows.
            cached.plaintext = b"\x00" * MASTER_KEY_SIZE_BYTES

    async def get_or_create(self, tenant_id: UUID) -> bytes:
        """Return the plaintext tenant KEK, creating it on first use.

        The plaintext is cached for ``cache_ttl_seconds``. Callers must
        treat the returned ``bytes`` as ephemeral — do not log, do not
        persist, and zero local copies promptly.
        """
        cached = self._cache.get(tenant_id)
        now = time.monotonic()
        if cached is not None and now - cached.cached_at < self._cache_ttl:
            return cached.plaintext

        # Atomic on-miss: only one coroutine may hit the DB for a given
        # tenant at a time. The lock is process-wide; cross-process
        # contention is handled by the INSERT ... ON CONFLICT below.
        async with self._lock:
            cached = self._cache.get(tenant_id)
            if cached is not None and now - cached.cached_at < self._cache_ttl:
                return cached.plaintext

            row = await self._fetch_or_insert(tenant_id)
            plaintext = await self._master.unwrap(
                row["kek_master_id"], bytes(row["wrapped_kek"])
            )
            if len(plaintext) != MASTER_KEY_SIZE_BYTES:
                raise RuntimeError(
                    f"tenant KEK is {len(plaintext)} bytes; expected "
                    f"{MASTER_KEY_SIZE_BYTES}. The wrapped row is corrupt."
                )
            self._cache[tenant_id] = _CachedKek(
                plaintext=plaintext,
                master_key_id=row["kek_master_id"],
                cached_at=time.monotonic(),
            )
            return plaintext

    async def _fetch_or_insert(self, tenant_id: UUID) -> asyncpg.Record:
        """Read the wrapped KEK row, INSERTing one on first use.

        Uses INSERT ... ON CONFLICT DO NOTHING to be safe under concurrent
        first-encrypts for the same tenant across processes. The follow-up
        SELECT is guaranteed to find a row.
        """
        async with self._pool.acquire() as conn:
            # Cross-tenant fetch is allowed here ONLY because the
            # crypto_writer role is granted exactly that surface. The
            # caller's identity is already authenticated upstream.
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT wrapped_kek, kek_master_id FROM tenant_keks WHERE tenant_id = $1",
                    tenant_id,
                )
                if row is not None:
                    return row

                # Generate a fresh KEK and wrap it under the current
                # master. The DB INSERT cannot run inside the lock above
                # because asyncpg pool acquisition can block on it.
                plaintext_kek = os.urandom(MASTER_KEY_SIZE_BYTES)
                try:
                    master_key_id, wrapped = await self._master.wrap(plaintext_kek)
                finally:
                    plaintext_kek = b"\x00" * MASTER_KEY_SIZE_BYTES  # noqa: F841

                await conn.execute(
                    """
                    INSERT INTO tenant_keks (tenant_id, wrapped_kek, kek_master_id)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (tenant_id) DO NOTHING
                    """,
                    tenant_id,
                    wrapped,
                    master_key_id,
                )
                # Re-fetch — either we just inserted, or another process
                # got there first; either way the row exists now.
                row = await conn.fetchrow(
                    "SELECT wrapped_kek, kek_master_id FROM tenant_keks WHERE tenant_id = $1",
                    tenant_id,
                )
                if row is None:  # pragma: no cover  — defensive
                    raise RuntimeError(
                        f"tenant KEK row missing for {tenant_id} immediately "
                        "after INSERT — Postgres role lacks SELECT privilege?"
                    )
                logger.info(
                    "tenant_kek.created",
                    extra={"tenant_id": str(tenant_id), "master_key_id": master_key_id},
                )
                return row
