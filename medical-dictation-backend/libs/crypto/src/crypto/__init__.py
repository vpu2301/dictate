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
- :class:`KmsMasterKeyProvider` — Vault-Transit-backed master (sprint 16).
- :class:`CompositeMasterKeyProvider` — mixed-master reads during re-wrap.
- :func:`build_master_key_provider` — the sanctioned composition helper.
- :class:`TenantKekRepository` — fetches plaintext tenant KEKs from `tenant_keks`.
- Exception classes for every failure mode.
"""

from __future__ import annotations

from .envelope import (
    ENVELOPE_ALGORITHM,
    ENVELOPE_VERSION,
    Envelope,
    EnvelopeBlob,
)
from .exceptions import (
    CryptoError,
    DecryptError,
    EnvelopeFormatError,
    MasterKeyError,
    MasterKeyPermissionError,
    TenantMismatchError,
)
from .ipn import (
    InvalidIpnError,
    IpnChecksumError,
    ipn_hmac,
    normalize_ipn,
    pack_ipn_envelope,
    unpack_ipn_envelope,
)
from .master import (
    CompositeMasterKeyProvider,
    FileMasterKeyProvider,
    KmsMasterKeyProvider,
    MasterKeyProvider,
    build_master_key_provider,
)
from .stream import encryptor_at_offset, fresh_stream_key, fresh_stream_nonce
from .tenant_kek import TenantKekRepository
from .vault_kv import fetch_kv_secrets

__all__ = [
    "CompositeMasterKeyProvider",
    "CryptoError",
    "DecryptError",
    "ENVELOPE_ALGORITHM",
    "ENVELOPE_VERSION",
    "Envelope",
    "EnvelopeBlob",
    "EnvelopeFormatError",
    "FileMasterKeyProvider",
    "InvalidIpnError",
    "IpnChecksumError",
    "KmsMasterKeyProvider",
    "MasterKeyError",
    "MasterKeyPermissionError",
    "MasterKeyProvider",
    "TenantKekRepository",
    "TenantMismatchError",
    "build_master_key_provider",
    "encryptor_at_offset",
    "fetch_kv_secrets",
    "fresh_stream_key",
    "fresh_stream_nonce",
    "ipn_hmac",
    "normalize_ipn",
    "pack_ipn_envelope",
    "unpack_ipn_envelope",
]
