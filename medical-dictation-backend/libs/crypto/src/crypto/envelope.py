"""Envelope encryption — the only sanctioned crypto path for PHI at rest.

Each :class:`EnvelopeBlob` records the per-object DEK (wrapped by the
tenant KEK), the per-object IV/tag, the tenant identifier, and the
master-key identifier under which the tenant KEK is wrapped. The blob is
serializable as JSON header + raw ciphertext (see ``libs/storage``).

Key safety properties:

- Every object uses a fresh DEK and IV; identical plaintexts produce
  distinct ciphertexts.
- AAD binds the ciphertext to the tenant_id, so cross-tenant blob mixups
  fail at GCM tag verification.
- ``decrypt`` ALSO performs a defence-in-depth check that
  ``blob.tenant_id == expected_tenant_id`` BEFORE touching crypto — this
  surfaces caller bugs early without consuming an AAD failure.
- DEK plaintext is zeroed on every code path that loads it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .exceptions import DecryptError, TenantMismatchError
from .master import GCM_IV_SIZE_BYTES, GCM_TAG_SIZE_BYTES, MasterKeyProvider
from .tenant_kek import TenantKekRepository

ENVELOPE_VERSION: Final = 1
ENVELOPE_ALGORITHM: Final = "AES-256-GCM"
DEK_SIZE_BYTES: Final = 32


@dataclass(frozen=True, slots=True)
class EnvelopeBlob:
    """Frozen record of all envelope material for one encrypted object.

    Persisted on the row that points at the ciphertext, or alongside the
    ciphertext in the object header (libs/storage handles the layout).

    The structure is wire-stable: changing it requires a version bump and
    a migration story for in-flight data.
    """

    ciphertext: bytes
    iv: bytes
    tag: bytes
    wrapped_dek: bytes
    dek_iv: bytes
    dek_tag: bytes
    tenant_id: UUID
    master_key_id: str
    algorithm: str = ENVELOPE_ALGORITHM
    version: int = ENVELOPE_VERSION
    # Optional caller-supplied AAD; reflected in metadata for forensic
    # reconstruction. Not secret, but ties the ciphertext to a specific
    # logical context (e.g. ``audio_id`` of the row).
    extra_aad: bytes | None = field(default=None)


class Envelope:
    """The single sanctioned encrypt/decrypt path for PHI.

    Construct with a master-key provider and a tenant-KEK repository.
    Reuse the instance across requests — both dependencies are async-safe.
    """

    def __init__(
        self,
        *,
        master_key_provider: MasterKeyProvider,
        kek_repository: TenantKekRepository,
    ) -> None:
        self._master = master_key_provider
        self._kek_repo = kek_repository

    async def encrypt(
        self,
        plaintext: bytes,
        *,
        tenant_id: UUID,
        aad: bytes | None = None,
    ) -> EnvelopeBlob:
        """Encrypt ``plaintext`` under a fresh DEK for ``tenant_id``.

        AAD passed to AES-GCM is ``tenant_id.bytes || (aad or b"")`` — the
        tenant binding is non-optional. Callers can pass an additional
        ``aad`` to bind the ciphertext to a logical context (e.g. the
        audio row id) and gain a check that the same DEK is not reused
        across contexts.
        """
        tenant_kek = await self._kek_repo.get_or_create(tenant_id)
        dek = os.urandom(DEK_SIZE_BYTES)
        iv = os.urandom(GCM_IV_SIZE_BYTES)
        dek_iv = os.urandom(GCM_IV_SIZE_BYTES)

        full_aad = _compose_aad(tenant_id, aad)
        try:
            cipher = AESGCM(dek)
            ct_and_tag = cipher.encrypt(iv, plaintext, full_aad)
            ciphertext, tag = ct_and_tag[:-GCM_TAG_SIZE_BYTES], ct_and_tag[-GCM_TAG_SIZE_BYTES:]

            kek_cipher = AESGCM(tenant_kek)
            wrapped = kek_cipher.encrypt(dek_iv, dek, full_aad)
            wrapped_dek, dek_tag = (
                wrapped[:-GCM_TAG_SIZE_BYTES],
                wrapped[-GCM_TAG_SIZE_BYTES:],
            )
        finally:
            # Best-effort zero. CPython doesn't guarantee no copies remain,
            # but overwriting the local reference is what we can do.
            dek = b"\x00" * DEK_SIZE_BYTES
            tenant_kek = b"\x00" * DEK_SIZE_BYTES  # noqa: F841

        return EnvelopeBlob(
            ciphertext=ciphertext,
            iv=iv,
            tag=tag,
            wrapped_dek=wrapped_dek,
            dek_iv=dek_iv,
            dek_tag=dek_tag,
            tenant_id=tenant_id,
            master_key_id=self._kek_repo.master_key_id_for(tenant_id),
            extra_aad=aad,
        )

    async def decrypt(
        self,
        blob: EnvelopeBlob,
        *,
        tenant_id: UUID,
        aad: bytes | None = None,
    ) -> bytes:
        """Decrypt ``blob`` for ``tenant_id``.

        Defence-in-depth: rejects on tenant_id mismatch BEFORE attempting
        any crypto operation. Even if an attacker manages to swap rows
        in the database, the explicit tenant check stops a confused-deputy
        attack from succeeding.
        """
        if blob.tenant_id != tenant_id:
            raise TenantMismatchError(
                f"envelope blob is for tenant {blob.tenant_id}; caller "
                f"context is {tenant_id}. Refusing to attempt crypto."
            )

        tenant_kek = await self._kek_repo.get_or_create(tenant_id)
        full_aad = _compose_aad(tenant_id, aad)
        try:
            kek_cipher = AESGCM(tenant_kek)
            try:
                dek = kek_cipher.decrypt(
                    blob.dek_iv,
                    blob.wrapped_dek + blob.dek_tag,
                    full_aad,
                )
            except InvalidTag as exc:
                raise DecryptError(
                    "DEK unwrap failed: tenant KEK / AAD / wrapped DEK mismatch."
                ) from exc

            try:
                cipher = AESGCM(dek)
                plaintext = cipher.decrypt(
                    blob.iv,
                    blob.ciphertext + blob.tag,
                    full_aad,
                )
            except InvalidTag as exc:
                raise DecryptError(
                    "ciphertext decrypt failed: tag mismatch (tampered or AAD did not match)."
                ) from exc
        finally:
            dek = b"\x00" * DEK_SIZE_BYTES  # noqa: F841
            tenant_kek = b"\x00" * DEK_SIZE_BYTES  # noqa: F841

        return plaintext


def _compose_aad(tenant_id: UUID, extra: bytes | None) -> bytes:
    """Build the AAD for both DEK-wrap and object-encrypt.

    ``tenant_id.bytes`` is the mandatory portion (16 bytes); any
    caller-supplied bytes are appended verbatim. We do NOT length-prefix
    the extra portion because we use the exact same construction on
    encrypt and decrypt — GCM AAD just needs to be equal on both sides.
    """
    return tenant_id.bytes + (extra or b"")
