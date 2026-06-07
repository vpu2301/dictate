"""EncryptedObjectStore round-trip + tamper tests, with an in-memory S3.

The S3-client surface is small; we substitute an in-memory dict so tests
run with no external dependencies. Integration tests against the real
Compose MinIO live under tests/integration/.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from crypto import (
    DecryptError,
    Envelope,
    EnvelopeFormatError,
    FileMasterKeyProvider,
    MasterKeyProvider,
    TenantMismatchError,
)
from crypto.master import MASTER_KEY_SIZE_BYTES
from storage import EncryptedObjectStore


class _InMemoryS3:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], bytes] = {}

    async def put_object(self, *, bucket: str, key: str, body: bytes) -> None:
        self._store[(bucket, key)] = body

    async def get_object(self, *, bucket: str, key: str) -> bytes:
        return self._store[(bucket, key)]

    async def delete_object(self, *, bucket: str, key: str) -> None:
        self._store.pop((bucket, key), None)

    async def head_bucket(self, bucket: str) -> None:
        return None

    async def generate_presigned_url(self, *, bucket: str, key: str, expires_in: int) -> str:
        return f"http://example/{bucket}/{key}?expires={expires_in}"

    async def aclose(self) -> None:
        return None

    # Test helper — tamper with a stored object.
    def tamper(self, bucket: str, key: str, offset: int, mask: int = 0x01) -> None:
        body = bytearray(self._store[(bucket, key)])
        body[offset] ^= mask
        self._store[(bucket, key)] = bytes(body)


class _InMemoryKekRepo:
    def __init__(self, master: MasterKeyProvider) -> None:
        self._master = master
        self._wrapped: dict[UUID, tuple[str, bytes]] = {}
        self._plain: dict[UUID, bytes] = {}

    def master_key_id_for(self, tenant_id: UUID) -> str:
        return self._wrapped[tenant_id][0]

    def evict(self, tenant_id: UUID) -> None:
        self._plain.pop(tenant_id, None)

    async def get_or_create(self, tenant_id: UUID) -> bytes:
        if tenant_id in self._plain:
            return self._plain[tenant_id]
        if tenant_id not in self._wrapped:
            kek = os.urandom(MASTER_KEY_SIZE_BYTES)
            mid, wrapped = await self._master.wrap(kek)
            self._wrapped[tenant_id] = (mid, wrapped)
            self._plain[tenant_id] = kek
            return kek
        mid, wrapped = self._wrapped[tenant_id]
        plain = await self._master.unwrap(mid, wrapped)
        self._plain[tenant_id] = plain
        return plain


@pytest.fixture
async def store(tmp_path: Path) -> EncryptedObjectStore:
    key_path = tmp_path / "master.key"
    key_path.write_bytes(os.urandom(MASTER_KEY_SIZE_BYTES))
    os.chmod(key_path, 0o400)
    master = FileMasterKeyProvider(path=key_path)
    await master.startup_self_check()
    repo = _InMemoryKekRepo(master)
    env = Envelope(master_key_provider=master, kek_repository=repo)  # type: ignore[arg-type]
    s3 = _InMemoryS3()
    return EncryptedObjectStore(s3=s3, bucket="mdx-test", envelope=env)


async def test_round_trip(store: EncryptedObjectStore) -> None:
    tid = uuid4()
    plaintext = b"phi audio bytes"
    await store.put(key="t/k1", plaintext=plaintext, tenant_id=tid)
    assert await store.get(key="t/k1", tenant_id=tid) == plaintext


async def test_round_trip_large(store: EncryptedObjectStore) -> None:
    tid = uuid4()
    plaintext = os.urandom(2 * 1024 * 1024)  # 2 MB
    await store.put(key="t/k2", plaintext=plaintext, tenant_id=tid)
    assert await store.get(key="t/k2", tenant_id=tid) == plaintext


async def test_round_trip_with_aad(store: EncryptedObjectStore) -> None:
    tid = uuid4()
    audio_id = uuid4()
    await store.put(key="t/k3", plaintext=b"phi", tenant_id=tid, aad=audio_id.bytes)
    assert await store.get(key="t/k3", tenant_id=tid, aad=audio_id.bytes) == b"phi"
    with pytest.raises(DecryptError):
        await store.get(key="t/k3", tenant_id=tid, aad=b"wrong-aad")


async def test_tampered_ciphertext_raises(store: EncryptedObjectStore) -> None:
    tid = uuid4()
    await store.put(key="t/k4", plaintext=b"x" * 100, tenant_id=tid)
    s3 = store._s3  # type: ignore[attr-defined]
    # Tamper a byte deep in the ciphertext (past the JSON header).
    # Skip the 4-byte prefix + header bytes; flip something in the body.
    body = s3._store[("mdx-test", "t/k4")]
    head_len = struct.unpack(">I", body[:4])[0]
    s3.tamper("mdx-test", "t/k4", offset=4 + head_len + 5)
    with pytest.raises(DecryptError):
        await store.get(key="t/k4", tenant_id=tid)


async def test_wrong_tenant_rejected_before_crypto(store: EncryptedObjectStore) -> None:
    a = uuid4()
    b = uuid4()
    await store.put(key="t/k5", plaintext=b"phi", tenant_id=a)
    with pytest.raises(TenantMismatchError):
        await store.get(key="t/k5", tenant_id=b)


async def test_corrupted_header_raises(store: EncryptedObjectStore) -> None:
    tid = uuid4()
    await store.put(key="t/k6", plaintext=b"x", tenant_id=tid)
    # Overwrite the header-length prefix with something absurd.
    s3 = store._s3  # type: ignore[attr-defined]
    body = bytearray(s3._store[("mdx-test", "t/k6")])
    body[:4] = struct.pack(">I", 0xFFFFFFFF)
    s3._store[("mdx-test", "t/k6")] = bytes(body)
    with pytest.raises(EnvelopeFormatError):
        await store.get(key="t/k6", tenant_id=tid)


async def test_presigned_url_format(store: EncryptedObjectStore) -> None:
    url = await store.presigned_url(key="t/k7", expires_in=300)
    assert "mdx-test/t/k7" in url
    assert "expires=300" in url
