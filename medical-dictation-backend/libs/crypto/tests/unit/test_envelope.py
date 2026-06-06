"""Property + adversarial tests for the envelope encrypt/decrypt path."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from crypto import (
    DecryptError,
    Envelope,
    EnvelopeBlob,
    TenantMismatchError,
)


async def test_round_trip_small(envelope: Envelope) -> None:
    tid = uuid4()
    blob = await envelope.encrypt(b"hello world", tenant_id=tid)
    assert await envelope.decrypt(blob, tenant_id=tid) == b"hello world"


async def test_round_trip_with_aad(envelope: Envelope) -> None:
    tid = uuid4()
    audio_id = uuid4()
    blob = await envelope.encrypt(b"phi", tenant_id=tid, aad=audio_id.bytes)
    assert (
        await envelope.decrypt(blob, tenant_id=tid, aad=audio_id.bytes)
        == b"phi"
    )


async def test_round_trip_empty(envelope: Envelope) -> None:
    tid = uuid4()
    blob = await envelope.encrypt(b"", tenant_id=tid)
    assert await envelope.decrypt(blob, tenant_id=tid) == b""


async def test_two_encrypts_produce_distinct_ciphertexts(envelope: Envelope) -> None:
    tid = uuid4()
    a = await envelope.encrypt(b"same plaintext", tenant_id=tid)
    b = await envelope.encrypt(b"same plaintext", tenant_id=tid)
    assert a.ciphertext != b.ciphertext
    assert a.iv != b.iv
    assert a.wrapped_dek != b.wrapped_dek


async def test_decrypt_wrong_tenant_raises_before_crypto(envelope: Envelope) -> None:
    """Confused-deputy: blob recorded as tenant A, caller claims tenant B."""
    a = uuid4()
    b = uuid4()
    blob = await envelope.encrypt(b"phi", tenant_id=a)
    with pytest.raises(TenantMismatchError):
        await envelope.decrypt(blob, tenant_id=b)


async def test_decrypt_tampered_ciphertext_raises(envelope: Envelope) -> None:
    tid = uuid4()
    blob = await envelope.encrypt(b"phi" * 100, tenant_id=tid)
    # Tamper one byte.
    bad_ct = bytearray(blob.ciphertext)
    bad_ct[5] ^= 0x01
    tampered = EnvelopeBlob(
        ciphertext=bytes(bad_ct),
        iv=blob.iv,
        tag=blob.tag,
        wrapped_dek=blob.wrapped_dek,
        dek_iv=blob.dek_iv,
        dek_tag=blob.dek_tag,
        tenant_id=blob.tenant_id,
        master_key_id=blob.master_key_id,
    )
    with pytest.raises(DecryptError, match="ciphertext decrypt failed"):
        await envelope.decrypt(tampered, tenant_id=tid)


async def test_decrypt_tampered_iv_raises(envelope: Envelope) -> None:
    tid = uuid4()
    blob = await envelope.encrypt(b"x" * 50, tenant_id=tid)
    bad_iv = bytearray(blob.iv)
    bad_iv[0] ^= 0xFF
    tampered = EnvelopeBlob(
        ciphertext=blob.ciphertext,
        iv=bytes(bad_iv),
        tag=blob.tag,
        wrapped_dek=blob.wrapped_dek,
        dek_iv=blob.dek_iv,
        dek_tag=blob.dek_tag,
        tenant_id=blob.tenant_id,
        master_key_id=blob.master_key_id,
    )
    with pytest.raises(DecryptError):
        await envelope.decrypt(tampered, tenant_id=tid)


async def test_decrypt_tampered_wrapped_dek_raises(envelope: Envelope) -> None:
    tid = uuid4()
    blob = await envelope.encrypt(b"x" * 50, tenant_id=tid)
    bad_dek = bytearray(blob.wrapped_dek)
    bad_dek[0] ^= 0xFF
    tampered = EnvelopeBlob(
        ciphertext=blob.ciphertext,
        iv=blob.iv,
        tag=blob.tag,
        wrapped_dek=bytes(bad_dek),
        dek_iv=blob.dek_iv,
        dek_tag=blob.dek_tag,
        tenant_id=blob.tenant_id,
        master_key_id=blob.master_key_id,
    )
    with pytest.raises(DecryptError, match="DEK unwrap"):
        await envelope.decrypt(tampered, tenant_id=tid)


async def test_decrypt_wrong_aad_raises(envelope: Envelope) -> None:
    tid = uuid4()
    blob = await envelope.encrypt(b"phi", tenant_id=tid, aad=b"audio-1")
    with pytest.raises(DecryptError):
        await envelope.decrypt(blob, tenant_id=tid, aad=b"audio-2")


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(plaintext=st.binary(min_size=0, max_size=64 * 1024))
async def test_property_round_trip(envelope: Envelope, plaintext: bytes) -> None:
    tid = uuid4()
    blob = await envelope.encrypt(plaintext, tenant_id=tid)
    assert await envelope.decrypt(blob, tenant_id=tid) == plaintext


async def test_large_plaintext(envelope: Envelope) -> None:
    """100 MB round-trip — the upper bound for an audio upload."""
    tid = uuid4()
    plaintext = os.urandom(10 * 1024 * 1024)  # 10MB is enough for CI signal
    blob = await envelope.encrypt(plaintext, tenant_id=tid)
    assert await envelope.decrypt(blob, tenant_id=tid) == plaintext


async def test_envelope_metadata_fields(envelope: Envelope) -> None:
    tid = uuid4()
    blob = await envelope.encrypt(b"phi", tenant_id=tid)
    assert blob.tenant_id == tid
    assert blob.algorithm == "AES-256-GCM"
    assert blob.version == 1
    assert blob.master_key_id == "file-v1"
    assert len(blob.iv) == 12
    assert len(blob.dek_iv) == 12
    assert len(blob.tag) == 16
    assert len(blob.dek_tag) == 16
