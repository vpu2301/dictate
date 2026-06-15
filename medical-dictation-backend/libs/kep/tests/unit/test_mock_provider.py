"""MockProvider end-to-end + production-refusal tests."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from medical_kep import MockProvider, ProviderName
from medical_kep.envelope import Envelope, EnvelopeFormat
from medical_kep.provider import (
    DocumentDisplayMetadata,
    InvalidCallbackError,
)

pytestmark = pytest.mark.asyncio


def _display() -> DocumentDisplayMetadata:
    return DocumentDisplayMetadata(
        title="t",
        report_code="REP-1",
        issuer_name="iss",
        encounter_date_iso="2026-05-10",
        page_count=1,
        sha256_hex="00" * 32,
        language="uk",
    )


async def test_production_refuses():
    with pytest.raises(RuntimeError):
        MockProvider(environment="production")
    with pytest.raises(RuntimeError):
        MockProvider(environment="prod")


async def test_dev_init_succeeds():
    p = MockProvider(environment="development")
    await p.aclose()


async def test_initiate_returns_redirect_url(tmp_path):
    p = MockProvider(environment="development", test_ca_dir=tmp_path)
    init = await p.initiate(
        document_pdf_hash=b"\x00" * 32,
        display=_display(),
        signer_hint=None,
        callback_url="http://localhost/cb",
    )
    assert init.provider == ProviderName.MOCK
    assert init.redirect_url.startswith("http://localhost:9999/mock-sign/")


async def test_callback_with_valid_hmac_produces_verifiable_envelope(tmp_path):
    p = MockProvider(environment="development", test_ca_dir=tmp_path)
    doc_hash = hashlib.sha256(b"hello").digest()
    init = await p.initiate(
        document_pdf_hash=doc_hash,
        display=_display(),
        signer_hint=None,
        callback_url="http://localhost/cb",
    )
    body = json.dumps(
        {
            "approved": True,
            "signer_full_name": "Лікар Тест",
            "signer_ipn": "1234567890",
        }
    ).encode("utf-8")
    sig = hmac.new(b"mock-callback-key", body, hashlib.sha256).hexdigest()
    envelope = await p.handle_callback(
        provider_session_id=init.provider_session_id,
        callback_body=body,
        callback_headers={"X-Mock-Signature": sig},
    )
    assert envelope.provider == ProviderName.MOCK
    # The DTO carries the *claimed* identity from the callback body
    # (used for display + audit). The structural parse reads the test
    # CA's static leaf cert, which has a fixed CN/IPN.
    assert envelope.parsed.signer_full_name == "Лікар Тест"
    assert envelope.parsed.signer_ipn == "1234567890"
    parsed = Envelope(envelope.signed_bytes).parse()
    assert parsed.document_hash_sha256 == doc_hash
    # Cert-bound identity (fixed by the test CA leaf):
    assert parsed.signer_full_name == "Test Clinician Leaf"
    assert parsed.signer_ipn == "1234567890"
    assert parsed.format == EnvelopeFormat.CADES_BES
    await p.aclose()


async def test_callback_with_invalid_hmac_rejected(tmp_path):
    p = MockProvider(environment="development", test_ca_dir=tmp_path)
    doc_hash = b"\x01" * 32
    init = await p.initiate(
        document_pdf_hash=doc_hash,
        display=_display(),
        signer_hint=None,
        callback_url="http://localhost/cb",
    )
    body = json.dumps({"approved": True}).encode("utf-8")
    with pytest.raises(InvalidCallbackError):
        await p.handle_callback(
            provider_session_id=init.provider_session_id,
            callback_body=body,
            callback_headers={"X-Mock-Signature": "deadbeef"},
        )


async def test_callback_rejected_when_not_approved(tmp_path):
    p = MockProvider(environment="development", test_ca_dir=tmp_path)
    doc_hash = b"\x02" * 32
    init = await p.initiate(
        document_pdf_hash=doc_hash,
        display=_display(),
        signer_hint=None,
        callback_url="http://localhost/cb",
    )
    body = json.dumps({"approved": False}).encode("utf-8")
    sig = hmac.new(b"mock-callback-key", body, hashlib.sha256).hexdigest()
    with pytest.raises(InvalidCallbackError, match="rejected"):
        await p.handle_callback(
            provider_session_id=init.provider_session_id,
            callback_body=body,
            callback_headers={"X-Mock-Signature": sig},
        )


async def test_health_returns_healthy(tmp_path):
    p = MockProvider(environment="development", test_ca_dir=tmp_path)
    h = await p.health()
    assert h.healthy is True
    assert h.provider == ProviderName.MOCK
