"""ІПН (РНОКПП) handling — normalization, checksum, HMAC, envelope packing.

The Ukrainian individual tax number (реєстраційний номер облікової картки
платника податків, РНОКПП, colloquially ІПН) is the national patient
identifier. It is PII of the highest sensitivity, so the platform never
stores or queries it raw:

- :func:`normalize_ipn` canonicalizes user input and validates the public
  РНОКПП checksum so typos are caught at capture time.
- :func:`ipn_hmac` produces the deterministic lookup token (HMAC-SHA256
  under a dedicated system key). This is the shared implementation used by
  both signing-service (signer identity, S09) and core-service (patient
  identity, S11) — with *independent* keys per space (ADR-0027).
- :func:`pack_ipn_envelope` / :func:`unpack_ipn_envelope` map an
  :class:`~crypto.envelope.EnvelopeBlob` to/from the two BYTEA columns
  (``ipn_encrypted``, ``ipn_dek``) used when the DPO enables raw-ІПН
  retention. Nulling ``ipn_dek`` crypto-shreds the raw value.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import re
from uuid import UUID

from secret import Secret

from .envelope import EnvelopeBlob
from .exceptions import CryptoError
from .master import GCM_IV_SIZE_BYTES, GCM_TAG_SIZE_BYTES

__all__ = [
    "InvalidIpnError",
    "IpnChecksumError",
    "ipn_hmac",
    "normalize_ipn",
    "pack_ipn_envelope",
    "unpack_ipn_envelope",
]


class InvalidIpnError(ValueError):
    """The value is not a well-formed 10-digit ІПН."""


class IpnChecksumError(InvalidIpnError):
    """The value is 10 digits but the РНОКПП control digit is wrong (typo)."""


_SEPARATORS = re.compile(r"[\s -]")

# Public РНОКПП control-digit weights for digits 1–9; the control digit is
# (Σ wᵢ·dᵢ mod 11) mod 10 and must equal digit 10.
_WEIGHTS = (-1, 5, 7, 9, 4, 6, 10, 5, 7)


def normalize_ipn(raw: str) -> str:
    """Canonicalize an ІПН: strip spaces/dashes, require exactly 10 ASCII
    digits, and verify the РНОКПП checksum.

    Raises :class:`InvalidIpnError` on shape violations and the narrower
    :class:`IpnChecksumError` when the shape is right but the control digit
    is not — the distinction lets the capture UI say "typo" instead of
    "not a number".
    """
    candidate = _SEPARATORS.sub("", raw or "")
    if len(candidate) != 10 or not candidate.isascii() or not candidate.isdigit():
        raise InvalidIpnError("ІПН must be exactly 10 digits")
    digits = [int(c) for c in candidate]
    control = sum(w * d for w, d in zip(_WEIGHTS, digits[:9], strict=True)) % 11 % 10
    if control != digits[9]:
        raise IpnChecksumError("ІПН control digit mismatch")
    return candidate


def ipn_hmac(ipn: str, key_hex: str | Secret[str]) -> bytes:
    """Deterministic lookup token: HMAC-SHA256(key, utf-8 ІПН).

    ``key_hex`` is the hex-encoded 32-byte system key, optionally wrapped in
    :class:`secret.Secret`. Callers pass the *normalized* ІПН.
    """
    material = key_hex.value() if isinstance(key_hex, Secret) else key_hex
    return _hmac.new(bytes.fromhex(material), ipn.encode("utf-8"), hashlib.sha256).digest()


# ── Envelope ⇄ column packing ────────────────────────────────────────
#
# The patients row stores the raw-ІПН envelope in two fixed-layout BYTEA
# columns instead of the libs/storage JSON header:
#
#   ipn_encrypted = iv(12) ‖ tag(16) ‖ ciphertext
#   ipn_dek       = dek_iv(12) ‖ dek_tag(16) ‖ wrapped_dek
#
# All segment sizes are AES-256-GCM constants, so the packing is
# deterministic and reversible without a header.

_PREFIX_LEN = GCM_IV_SIZE_BYTES + GCM_TAG_SIZE_BYTES


def pack_ipn_envelope(blob: EnvelopeBlob) -> tuple[bytes, bytes]:
    """Return the ``(ipn_encrypted, ipn_dek)`` column pair for ``blob``."""
    ipn_encrypted = blob.iv + blob.tag + blob.ciphertext
    ipn_dek = blob.dek_iv + blob.dek_tag + blob.wrapped_dek
    return ipn_encrypted, ipn_dek


def unpack_ipn_envelope(
    *,
    ipn_encrypted: bytes,
    ipn_dek: bytes,
    tenant_id: UUID,
) -> EnvelopeBlob:
    """Rebuild the :class:`EnvelopeBlob` from the column pair.

    ``master_key_id`` is intentionally left blank: :meth:`Envelope.decrypt`
    never consults it (the KEK repository resolves the active master key),
    and persisting it per-row would add a column for forensic metadata the
    object-store path already records elsewhere.
    """
    if len(ipn_encrypted) <= _PREFIX_LEN or len(ipn_dek) <= _PREFIX_LEN:
        raise CryptoError("ІПН envelope columns are too short to carry iv‖tag‖payload")
    return EnvelopeBlob(
        ciphertext=ipn_encrypted[_PREFIX_LEN:],
        iv=ipn_encrypted[:GCM_IV_SIZE_BYTES],
        tag=ipn_encrypted[GCM_IV_SIZE_BYTES:_PREFIX_LEN],
        wrapped_dek=ipn_dek[_PREFIX_LEN:],
        dek_iv=ipn_dek[:GCM_IV_SIZE_BYTES],
        dek_tag=ipn_dek[GCM_IV_SIZE_BYTES:_PREFIX_LEN],
        tenant_id=tenant_id,
        master_key_id="",
    )
