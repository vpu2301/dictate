"""Exception hierarchy for libs/crypto.

Distinct classes so callers can branch on failure mode and so security
metrics can be attributed cleanly. Never expose plaintext or key bytes
in any exception message.
"""

from __future__ import annotations


class CryptoError(Exception):
    """Base class for every libs/crypto failure."""


class MasterKeyError(CryptoError):
    """The master key file is missing, malformed, or otherwise unusable."""


class MasterKeyPermissionError(MasterKeyError):
    """Master key file has overly-permissive mode (must be ≤ 0400)."""


class EnvelopeFormatError(CryptoError):
    """The serialized envelope blob is malformed (bad header, truncated, …)."""


class DecryptError(CryptoError):
    """Decryption failed — wrong key, tampered ciphertext, bad AAD, or
    GCM tag mismatch. Message never includes plaintext or key material."""


class TenantMismatchError(CryptoError):
    """The envelope's recorded ``tenant_id`` does not match the caller's
    expected tenant. Defends against confused-deputy attacks where one
    tenant's blob is decrypted under another tenant's context."""
