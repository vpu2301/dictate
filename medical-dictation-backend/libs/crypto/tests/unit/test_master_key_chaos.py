"""Master-key chaos (spec §4.8): the worker must FAIL CLOSED at startup if
the master key file is missing, renamed away, the wrong size, or has loose
permissions — each with a precise error that points an operator to the runbook.

These run as ordinary unit tests (no infra): they drive
:meth:`FileMasterKeyProvider.startup_self_check` against temp files, which is
exactly the check ``asr-worker``'s ``build_state`` runs before any traffic.
"""

from __future__ import annotations

import os

import pytest

from crypto import (
    FileMasterKeyProvider,
    MasterKeyError,
    MasterKeyPermissionError,
)


async def test_missing_master_key_refuses_startup(tmp_path) -> None:
    missing = tmp_path / "master.key"  # never created
    provider = FileMasterKeyProvider(path=missing)
    with pytest.raises(MasterKeyError, match="not found"):
        await provider.startup_self_check()


async def test_renamed_master_key_refuses_startup(tmp_path) -> None:
    """The §4.8 demo: a present, valid key passes; rename it away and a fresh
    provider over the same path refuses to start."""
    key = tmp_path / "master.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o400)

    provider = FileMasterKeyProvider(path=key)
    await provider.startup_self_check()  # green while present + well-formed

    key.rename(tmp_path / "master.key.bak")
    after_rename = FileMasterKeyProvider(path=key)
    with pytest.raises(MasterKeyError, match="not found"):
        await after_rename.startup_self_check()


async def test_wrong_size_master_key_refuses_startup(tmp_path) -> None:
    key = tmp_path / "master.key"
    key.write_bytes(os.urandom(16))  # AES-128 length, not the required 32
    key.chmod(0o400)
    provider = FileMasterKeyProvider(path=key)
    with pytest.raises(MasterKeyError, match="32 bytes"):
        await provider.startup_self_check()


async def test_loose_permissions_master_key_refuses_startup(tmp_path) -> None:
    key = tmp_path / "master.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o444)  # group/other can read — too permissive for a master key
    provider = FileMasterKeyProvider(path=key)
    with pytest.raises(MasterKeyPermissionError, match="0400"):
        await provider.startup_self_check()


async def test_runbook_pointer_in_missing_key_error(tmp_path) -> None:
    """Operators must be able to act without paging on-call: the error names
    the runbook section, per spec §6 fail-closed requirement."""
    provider = FileMasterKeyProvider(path=tmp_path / "nope.key")
    with pytest.raises(MasterKeyError, match="asr-worker.md"):
        await provider.startup_self_check()
