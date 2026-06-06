"""Envelope verification — end-to-end through MockProvider."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from medical_kep import MockProvider
from medical_kep.envelope import Envelope
from medical_kep.provider import DocumentDisplayMetadata
from medical_kep.trust_store import TrustStore
from medical_kep.verify import verify_envelope


pytestmark = pytest.mark.asyncio


def _display() -> DocumentDisplayMetadata:
    return DocumentDisplayMetadata(
        title="t", report_code="REP-1", issuer_name="iss",
        encounter_date_iso="2026-05-10", page_count=1,
        sha256_hex="00" * 32, language="uk",
    )


def _trust_store_for(test_ca_dir: Path) -> TrustStore:
    """Construct a TrustStore that recognises the mock test CA."""
    # Convert the cryptography-format ca.cert.pem into our trust-store
    # layout (test-ca-bundle.pem) and load with include_test_ca=True.
    bundle = test_ca_dir / "test-ca-bundle.pem"
    bundle.write_bytes((test_ca_dir / "ca.cert.pem").read_bytes())
    return TrustStore.load_from_dir(test_ca_dir, include_test_ca=True)


async def test_signed_envelope_verifies_against_test_ca(tmp_path):
    p = MockProvider(environment="development", test_ca_dir=tmp_path)
    doc_hash = hashlib.sha256(b"contents").digest()
    init = await p.initiate(
        document_pdf_hash=doc_hash, display=_display(),
        signer_hint=None, callback_url="http://localhost/cb",
    )
    body = json.dumps({
        "approved": True,
        "signer_full_name": "Лікар Тест",
        "signer_ipn": "9876543210",
    }).encode("utf-8")
    sig = hmac.new(b"mock-callback-key", body, hashlib.sha256).hexdigest()
    envelope = await p.handle_callback(
        provider_session_id=init.provider_session_id,
        callback_body=body,
        callback_headers={"X-Mock-Signature": sig},
    )
    parsed = Envelope(envelope.signed_bytes).parse()
    trust = _trust_store_for(tmp_path)

    result = verify_envelope(
        parsed=parsed,
        expected_document_hash=doc_hash,
        trust_store=trust,
    )
    assert result.valid is True, result.errors
    # ``signer_full_name`` is the cert-bound identity (the test CA leaf),
    # NOT the claim in the callback body.
    assert result.signer_full_name == "Test Clinician Leaf"
    # Test-anchored chains can NEVER be qualified, regardless of cert flags.
    assert result.is_qualified is False


async def test_wrong_document_hash_fails(tmp_path):
    p = MockProvider(environment="development", test_ca_dir=tmp_path)
    doc_hash = hashlib.sha256(b"original").digest()
    init = await p.initiate(
        document_pdf_hash=doc_hash, display=_display(),
        signer_hint=None, callback_url="http://localhost/cb",
    )
    body = json.dumps({"approved": True}).encode("utf-8")
    sig = hmac.new(b"mock-callback-key", body, hashlib.sha256).hexdigest()
    envelope = await p.handle_callback(
        provider_session_id=init.provider_session_id,
        callback_body=body,
        callback_headers={"X-Mock-Signature": sig},
    )
    parsed = Envelope(envelope.signed_bytes).parse()
    trust = _trust_store_for(tmp_path)
    result = verify_envelope(
        parsed=parsed,
        expected_document_hash=hashlib.sha256(b"tampered").digest(),
        trust_store=trust,
    )
    assert result.valid is False
    assert "document_hash_mismatch" in result.errors


async def test_no_trust_anchor_fails(tmp_path):
    p = MockProvider(environment="development", test_ca_dir=tmp_path)
    doc_hash = hashlib.sha256(b"x").digest()
    init = await p.initiate(
        document_pdf_hash=doc_hash, display=_display(),
        signer_hint=None, callback_url="http://localhost/cb",
    )
    body = json.dumps({"approved": True}).encode("utf-8")
    sig = hmac.new(b"mock-callback-key", body, hashlib.sha256).hexdigest()
    envelope = await p.handle_callback(
        provider_session_id=init.provider_session_id,
        callback_body=body,
        callback_headers={"X-Mock-Signature": sig},
    )
    parsed = Envelope(envelope.signed_bytes).parse()
    empty_trust = TrustStore(ca_certs=[], tsa_certs=[])
    result = verify_envelope(
        parsed=parsed,
        expected_document_hash=doc_hash,
        trust_store=empty_trust,
    )
    assert result.valid is False
    assert "untrusted_certificate_chain" in result.errors
