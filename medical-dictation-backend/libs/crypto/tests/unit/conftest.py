"""Shared fixtures for libs/crypto unit tests.

Uses an in-memory ``TenantKekRepository`` substitute so tests don't need
a live Postgres instance. The real ``TenantKekRepository`` is exercised
in integration tests under ``tests/integration/``.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from crypto import (
    Envelope,
    FileMasterKeyProvider,
    TenantKekRepository,
)
from crypto.master import MASTER_KEY_SIZE_BYTES, MasterKeyProvider


class InMemoryKekRepo:
    """Drop-in ``TenantKekRepository`` substitute with no DB dependency."""

    def __init__(self, master: MasterKeyProvider) -> None:
        self._master = master
        self._wrapped: dict[UUID, tuple[str, bytes]] = {}
        self._plaintext: dict[UUID, bytes] = {}

    def master_key_id_for(self, tenant_id: UUID) -> str:
        return self._wrapped[tenant_id][0]

    def evict(self, tenant_id: UUID) -> None:
        self._plaintext.pop(tenant_id, None)

    async def get_or_create(self, tenant_id: UUID) -> bytes:
        cached = self._plaintext.get(tenant_id)
        if cached is not None:
            return cached
        if tenant_id not in self._wrapped:
            kek = os.urandom(MASTER_KEY_SIZE_BYTES)
            mid, wrapped = await self._master.wrap(kek)
            self._wrapped[tenant_id] = (mid, wrapped)
            self._plaintext[tenant_id] = kek
            return kek
        mid, wrapped = self._wrapped[tenant_id]
        plain = await self._master.unwrap(mid, wrapped)
        self._plaintext[tenant_id] = plain
        return plain


@pytest.fixture
def tmp_master_key(tmp_path: Path) -> Path:
    """Create a 0400 master.key file in a tmpdir."""
    key_path = tmp_path / "master.key"
    key_path.write_bytes(os.urandom(MASTER_KEY_SIZE_BYTES))
    os.chmod(key_path, 0o400)
    return key_path


@pytest.fixture
async def master_provider(tmp_master_key: Path) -> FileMasterKeyProvider:
    provider = FileMasterKeyProvider(path=tmp_master_key)
    await provider.startup_self_check()
    return provider


@pytest.fixture
async def envelope(master_provider: FileMasterKeyProvider) -> Envelope:
    repo = InMemoryKekRepo(master_provider)
    # Mypy: TenantKekRepository's class is what production uses, but in
    # tests we substitute. The Envelope's constructor accepts anything
    # that quacks like ``master_key_id_for`` + ``get_or_create``.
    return Envelope(master_key_provider=master_provider, kek_repository=repo)  # type: ignore[arg-type]
