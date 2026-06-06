"""Master-key providers.

A master-key provider knows how to ``wrap`` (encrypt) a plaintext tenant KEK
into ciphertext suitable for at-rest storage, and ``unwrap`` it back. The
sprint-03 production stack ships ``FileMasterKeyProvider``; sprint 16 swaps
in ``KmsMasterKeyProvider`` for AWS KMS / Hashicorp Vault.

Why the indirection? The wrapping mechanism changes across environments,
but the envelope contract (per-object DEK, per-tenant KEK, master KEK)
must not change. Pinning the Protocol now means sprint 16's KMS migration
is a 1-line swap in the service composition, plus the re-wrap procedure.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .exceptions import DecryptError, MasterKeyError, MasterKeyPermissionError

logger = logging.getLogger(__name__)

# 32 bytes = AES-256-GCM key.
MASTER_KEY_SIZE_BYTES: int = 32
GCM_IV_SIZE_BYTES: int = 12
GCM_TAG_SIZE_BYTES: int = 16

# Deterministic associated data for KEK-wrapping. Pinning this to a
# version-tagged byte string means future format changes don't silently
# decrypt against the old format — they fail closed.
MASTER_WRAP_AAD: bytes = b"mdx-master-kek-v1"

# Marker the FileMasterKeyProvider returns. KMS provider returns the KMS
# key ARN. Stored in EnvelopeBlob so future re-wrap migrations know which
# master a tenant KEK was wrapped under.
FILE_MASTER_KEY_ID: str = "file-v1"


class MasterKeyProvider(Protocol):
    """Wrap / unwrap a tenant KEK under the environment's master key.

    Implementations MUST be safe to call from multiple coroutines
    concurrently. They MUST NOT expose plaintext master-key bytes via
    their public API.
    """

    async def wrap(self, kek_plaintext: bytes) -> tuple[str, bytes]:
        """Wrap a 32-byte tenant KEK.

        Returns ``(master_key_id, wrapped_kek)``. ``wrapped_kek`` is
        ``iv || ciphertext || tag`` (12 + N + 16 bytes for AES-GCM).
        """
        ...

    async def unwrap(self, master_key_id: str, wrapped_kek: bytes) -> bytes:
        """Unwrap to plaintext tenant KEK. Caller must zero the result
        when finished. Raises :class:`DecryptError` on tag mismatch."""
        ...


class FileMasterKeyProvider:
    """Read the master key from a 0400-mode file on disk.

    Production swaps this for :class:`KmsMasterKeyProvider`. The file path
    is configurable via ``MDX_MASTER_KEY_PATH``; in dev compose it's
    bind-mounted from ``infra/dev/master.key``.

    Startup self-check: refuses to operate if the file mode is more
    permissive than 0400, if the file is missing, or if it's the wrong
    length. The check happens in :meth:`startup_self_check`, which the
    service lifespan calls before any traffic is accepted.
    """

    def __init__(self, *, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._aead: AESGCM | None = None  # lazily loaded; cleared on rotate

    @property
    def master_key_id(self) -> str:
        return FILE_MASTER_KEY_ID

    async def startup_self_check(self) -> None:
        """Verify the master key file's existence, mode, and length.

        Called from each service's lifespan ``startup``. Raises a precise
        :class:`MasterKeyError` subclass that points to the runbook so an
        operator can act without paging the on-call engineer.
        """
        if not self._path.exists():
            raise MasterKeyError(
                f"master key file not found at {self._path!s}. "
                "See docs/runbooks/asr-worker.md § master-key-missing."
            )
        try:
            st = self._path.stat()
        except OSError as exc:
            raise MasterKeyError(
                f"master key file at {self._path!s} cannot be stat()'d: "
                f"{type(exc).__name__}"
            ) from exc

        # Accept any mode whose permission bits are a SUBSET of 0400.
        # That is: no group/other access, and no write-by-owner.
        mode_bits = stat.S_IMODE(st.st_mode)
        if mode_bits & ~0o400:
            raise MasterKeyPermissionError(
                f"master key file at {self._path!s} has mode {oct(mode_bits)}; "
                "must be 0400 (read-only by owner). See "
                "docs/runbooks/asr-worker.md § master-key-permissions."
            )

        if st.st_size != MASTER_KEY_SIZE_BYTES:
            raise MasterKeyError(
                f"master key file at {self._path!s} is {st.st_size} bytes; "
                f"expected exactly {MASTER_KEY_SIZE_BYTES} bytes for AES-256."
            )

        # Load once and keep AESGCM around for the process lifetime.
        # We deliberately do NOT hold the raw 32-byte key in a Python str;
        # the AESGCM instance owns the bytes internally.
        with self._path.open("rb") as f:
            raw = f.read(MASTER_KEY_SIZE_BYTES)
        try:
            self._aead = AESGCM(raw)
        finally:
            # Best-effort zero — Python doesn't guarantee no copies, but
            # we at least overwrite our local reference.
            raw = b"\x00" * MASTER_KEY_SIZE_BYTES

        logger.info(
            "master_key.loaded",
            extra={
                "master_key_id": self.master_key_id,
                "path": str(self._path),
                "mode": oct(mode_bits),
            },
        )

    def _aead_or_raise(self) -> AESGCM:
        if self._aead is None:
            raise MasterKeyError(
                "master key not loaded. Call startup_self_check() before use."
            )
        return self._aead

    async def wrap(self, kek_plaintext: bytes) -> tuple[str, bytes]:
        if len(kek_plaintext) != 32:
            raise MasterKeyError(
                f"tenant KEK must be 32 bytes, got {len(kek_plaintext)}"
            )
        iv = os.urandom(GCM_IV_SIZE_BYTES)
        ct = self._aead_or_raise().encrypt(iv, kek_plaintext, MASTER_WRAP_AAD)
        # cryptography's AESGCM returns ciphertext || tag concatenated;
        # we prepend the IV so the on-disk format is iv || ct || tag.
        return FILE_MASTER_KEY_ID, iv + ct

    async def unwrap(self, master_key_id: str, wrapped_kek: bytes) -> bytes:
        if master_key_id != FILE_MASTER_KEY_ID:
            raise MasterKeyError(
                f"master_key_id {master_key_id!r} is not handled by "
                "FileMasterKeyProvider. Sprint 16 introduces multi-master "
                "support via KmsMasterKeyProvider."
            )
        if len(wrapped_kek) < GCM_IV_SIZE_BYTES + GCM_TAG_SIZE_BYTES:
            raise DecryptError("wrapped KEK is too short to be valid")
        iv, ct = wrapped_kek[:GCM_IV_SIZE_BYTES], wrapped_kek[GCM_IV_SIZE_BYTES:]
        try:
            return self._aead_or_raise().decrypt(iv, ct, MASTER_WRAP_AAD)
        except InvalidTag as exc:
            raise DecryptError(
                "master-key unwrap failed: GCM tag mismatch. The wrapped KEK "
                "may have been tampered with, or the master key has rotated."
            ) from exc


class KmsMasterKeyProvider:
    """Stub for sprint 16 (KMS-backed master keys).

    Constructing this raises ``NotImplementedError`` so a misconfigured
    deployment can't silently fall back to a no-op provider. Sprint 16
    swaps the body in; the Protocol signature is the contract.
    """

    def __init__(self, *, key_arn: str) -> None:
        raise NotImplementedError(
            "KmsMasterKeyProvider lands in sprint 16. Use FileMasterKeyProvider "
            "for now and see ADR-0011 for the migration plan."
        )

    async def wrap(self, kek_plaintext: bytes) -> tuple[str, bytes]:  # pragma: no cover
        raise NotImplementedError

    async def unwrap(
        self, master_key_id: str, wrapped_kek: bytes
    ) -> bytes:  # pragma: no cover
        raise NotImplementedError
