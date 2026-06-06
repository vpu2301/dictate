"""libs/crypto — envelope encryption for PHI at rest.

Three-layer hierarchy (see ADR-0011):

    KEK_master      — single key per environment, mounted from disk or KMS.
                      Wraps every tenant KEK.
    KEK_tenant      — one per tenant; lives in `tenant_keks` table, wrapped.
                      Wraps every per-object DEK.
    DEK_object      — fresh per object; never persisted. Wrapped by tenant KEK.

Public surface:

- :class:`EnvelopeBlob`        — frozen record of all envelope material.
- :class:`Envelope`            — the single sanctioned encrypt/decrypt path.
- :class:`MasterKeyProvider`   — Protocol; ``FileMasterKeyProvider`` for dev.
- :class:`FileMasterKeyProvider`
- :class:`KmsMasterKeyProvider` — stub; sprint 16 implements.
- :class:`TenantKekRepository` — fetches plaintext tenant KEKs from `tenant_keks`.
- Exception classes for every failure mode.
"""

from __future__ import annotations

from .envelope import (
    Envelope,
    EnvelopeBlob,
    ENVELOPE_ALGORITHM,
    ENVELOPE_VERSION,
)
from .exceptions import (
    CryptoError,
    DecryptError,
    EnvelopeFormatError,
    MasterKeyError,
    MasterKeyPermissionError,
    TenantMismatchError,
)
from .master import (
    FileMasterKeyProvider,
    KmsMasterKeyProvider,
    MasterKeyProvider,
)
from .stream import encryptor_at_offset, fresh_stream_key, fresh_stream_nonce
from .tenant_kek import TenantKekRepository

__all__ = [
    "CryptoError",
    "DecryptError",
    "ENVELOPE_ALGORITHM",
    "ENVELOPE_VERSION",
    "Envelope",
    "EnvelopeBlob",
    "EnvelopeFormatError",
    "FileMasterKeyProvider",
    "KmsMasterKeyProvider",
    "MasterKeyError",
    "MasterKeyPermissionError",
    "MasterKeyProvider",
    "TenantKekRepository",
    "TenantMismatchError",
    "encryptor_at_offset",
    "fresh_stream_key",
    "fresh_stream_nonce",
]
