"""Adversarial tests for FileMasterKeyProvider self-check + wrap/unwrap."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crypto import (
    DecryptError,
    FileMasterKeyProvider,
    MasterKeyError,
    MasterKeyPermissionError,
)
from crypto.master import MASTER_KEY_SIZE_BYTES


async def test_self_check_rejects_missing_file(tmp_path: Path) -> None:
    provider = FileMasterKeyProvider(path=tmp_path / "does-not-exist")
    with pytest.raises(MasterKeyError, match="not found"):
        await provider.startup_self_check()


async def test_self_check_rejects_world_readable_mode(tmp_path: Path) -> None:
    key_path = tmp_path / "master.key"
    key_path.write_bytes(os.urandom(MASTER_KEY_SIZE_BYTES))
    os.chmod(key_path, 0o644)
    provider = FileMasterKeyProvider(path=key_path)
    with pytest.raises(MasterKeyPermissionError):
        await provider.startup_self_check()


async def test_self_check_rejects_owner_writable(tmp_path: Path) -> None:
    key_path = tmp_path / "master.key"
    key_path.write_bytes(os.urandom(MASTER_KEY_SIZE_BYTES))
    # 0600 is also rejected — only ≤ 0400 is acceptable.
    os.chmod(key_path, 0o600)
    provider = FileMasterKeyProvider(path=key_path)
    with pytest.raises(MasterKeyPermissionError):
        await provider.startup_self_check()


async def test_self_check_rejects_wrong_length(tmp_path: Path) -> None:
    key_path = tmp_path / "master.key"
    key_path.write_bytes(b"\x00" * (MASTER_KEY_SIZE_BYTES + 1))
    os.chmod(key_path, 0o400)
    provider = FileMasterKeyProvider(path=key_path)
    with pytest.raises(MasterKeyError, match="bytes"):
        await provider.startup_self_check()


async def test_wrap_unwrap_round_trip(tmp_master_key: Path) -> None:
    provider = FileMasterKeyProvider(path=tmp_master_key)
    await provider.startup_self_check()
    kek = os.urandom(MASTER_KEY_SIZE_BYTES)
    mid, wrapped = await provider.wrap(kek)
    assert mid == "file-v1"
    assert wrapped != kek
    assert await provider.unwrap(mid, wrapped) == kek


async def test_unwrap_rejects_wrong_master_id(tmp_master_key: Path) -> None:
    provider = FileMasterKeyProvider(path=tmp_master_key)
    await provider.startup_self_check()
    kek = os.urandom(MASTER_KEY_SIZE_BYTES)
    _mid, wrapped = await provider.wrap(kek)
    with pytest.raises(MasterKeyError, match="not handled"):
        await provider.unwrap("kms-arn:future-provider", wrapped)


async def test_unwrap_detects_tampering(tmp_master_key: Path) -> None:
    provider = FileMasterKeyProvider(path=tmp_master_key)
    await provider.startup_self_check()
    kek = os.urandom(MASTER_KEY_SIZE_BYTES)
    mid, wrapped = await provider.wrap(kek)
    # Flip a single byte in the ciphertext portion (after the 12-byte IV).
    tampered = bytearray(wrapped)
    tampered[20] ^= 0x01
    with pytest.raises(DecryptError, match="tag mismatch"):
        await provider.unwrap(mid, bytes(tampered))


async def test_wrap_rejects_non_32_byte_kek(tmp_master_key: Path) -> None:
    provider = FileMasterKeyProvider(path=tmp_master_key)
    await provider.startup_self_check()
    with pytest.raises(MasterKeyError, match="32 bytes"):
        await provider.wrap(b"\x00" * 16)


async def test_unwrap_rejects_truncated_wrapped(tmp_master_key: Path) -> None:
    provider = FileMasterKeyProvider(path=tmp_master_key)
    await provider.startup_self_check()
    with pytest.raises(DecryptError, match="too short"):
        await provider.unwrap("file-v1", b"x" * 5)
