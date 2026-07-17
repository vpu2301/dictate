"""ІПН helpers — normalization, РНОКПП checksum, HMAC, envelope packing.

All ІПН values here are synthetic: digit strings constructed to satisfy
(or violate) the public control-digit formula. None belong to a person.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
from uuid import UUID

import pytest

from crypto import (
    Envelope,
    InvalidIpnError,
    IpnChecksumError,
    ipn_hmac,
    normalize_ipn,
    pack_ipn_envelope,
    unpack_ipn_envelope,
)
from crypto.exceptions import CryptoError
from secret import Secret

TENANT = UUID("00000000-0000-0000-0000-0000000000aa")
PATIENT = UUID("33333333-3333-3333-3333-333333333333")

# Synthetic vectors with a hand-verified control digit:
#   weights (-1,5,7,9,4,6,10,5,7) · digits[0:9]  →  (Σ mod 11) mod 10 == digit 10
VALID_IPNS = [
    "1759013776",  # Σ=270, 270 % 11 = 6
    "2874309631",  # Σ=276, 276 % 11 = 1
    "1759113770",  # Σ=274, 274 % 11 = 10 → control (10 % 10) = 0 edge case
    "0000000000",  # Σ=0 → control 0 (degenerate but formula-valid)
]


# ── normalize + checksum ────────────────────────────────────────────


@pytest.mark.parametrize("ipn", VALID_IPNS)
def test_valid_vectors_accepted(ipn: str) -> None:
    assert normalize_ipn(ipn) == ipn


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("175 901 37 76", "1759013776"),
        ("1759-0137-76", "1759013776"),
        (" 1759013776 ", "1759013776"),
        ("28 74-30 96-31", "2874309631"),
    ],
)
def test_normalize_strips_separators(raw: str, expected: str) -> None:
    assert normalize_ipn(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "175901377",  # 9 digits
        "17590137761",  # 11 digits
        "175901377a",  # letter
        "",  # empty
        "１７５９０１３７７６",  # full-width unicode digits — not ASCII
    ],
)
def test_shape_violations_raise_invalid(raw: str) -> None:
    with pytest.raises(InvalidIpnError):
        normalize_ipn(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "1759013775",  # control digit off by one (typo)
        "1759013777",
        "2874309639",
    ],
)
def test_checksum_violations_raise_distinct_error(raw: str) -> None:
    with pytest.raises(IpnChecksumError):
        normalize_ipn(raw)


def test_checksum_error_is_an_invalid_ipn_error() -> None:
    # Callers that don't care about the distinction catch one type.
    assert issubclass(IpnChecksumError, InvalidIpnError)


# ── hmac ────────────────────────────────────────────────────────────

KEY_A = "0a" * 32
KEY_B = "0b" * 32


def test_hmac_deterministic() -> None:
    assert ipn_hmac("1759013776", KEY_A) == ipn_hmac("1759013776", KEY_A)


def test_hmac_key_separation() -> None:
    assert ipn_hmac("1759013776", KEY_A) != ipn_hmac("1759013776", KEY_B)


def test_hmac_input_separation() -> None:
    assert ipn_hmac("1759013776", KEY_A) != ipn_hmac("2874309631", KEY_A)


def test_hmac_accepts_secret_wrapped_key() -> None:
    assert ipn_hmac("1759013776", Secret(KEY_A)) == ipn_hmac("1759013776", KEY_A)


def test_hmac_matches_reference_construction() -> None:
    # Byte-for-byte the S09 signing-service construction — moving the helper
    # to libs/crypto must not change any stored signer_ipn_hmac.
    expected = _hmac.new(
        bytes.fromhex(KEY_A), b"1759013776", hashlib.sha256
    ).digest()
    assert ipn_hmac("1759013776", KEY_A) == expected


# ── envelope packing round-trip ─────────────────────────────────────


async def test_pack_unpack_decrypt_round_trip(envelope: Envelope) -> None:
    blob = await envelope.encrypt(
        b"1759013776", tenant_id=TENANT, aad=PATIENT.bytes
    )
    ipn_encrypted, ipn_dek = pack_ipn_envelope(blob)

    rebuilt = unpack_ipn_envelope(
        ipn_encrypted=ipn_encrypted, ipn_dek=ipn_dek, tenant_id=TENANT
    )
    plaintext = await envelope.decrypt(rebuilt, tenant_id=TENANT, aad=PATIENT.bytes)
    assert plaintext == b"1759013776"


async def test_unpack_rejects_wrong_aad(envelope: Envelope) -> None:
    from crypto.exceptions import DecryptError

    blob = await envelope.encrypt(b"1759013776", tenant_id=TENANT, aad=PATIENT.bytes)
    ipn_encrypted, ipn_dek = pack_ipn_envelope(blob)
    rebuilt = unpack_ipn_envelope(
        ipn_encrypted=ipn_encrypted, ipn_dek=ipn_dek, tenant_id=TENANT
    )
    other_patient = UUID("44444444-4444-4444-4444-444444444444")
    with pytest.raises(DecryptError):
        await envelope.decrypt(rebuilt, tenant_id=TENANT, aad=other_patient.bytes)


def test_unpack_rejects_truncated_columns() -> None:
    with pytest.raises(CryptoError):
        unpack_ipn_envelope(ipn_encrypted=b"short", ipn_dek=b"x" * 64, tenant_id=TENANT)
    with pytest.raises(CryptoError):
        unpack_ipn_envelope(ipn_encrypted=b"x" * 64, ipn_dek=b"short", tenant_id=TENANT)
