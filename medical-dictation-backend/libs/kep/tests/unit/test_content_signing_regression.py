"""Sprint-13 signing regression — envelopes over report canonical bytes.

Sprint-09 envelopes commit (via the document hash) to
``report_models.canonical_content_bytes``. This guard proves:

1. an envelope signed over an OLD-shape content (pre-S13, empty
   ``field_specific_metadata``) still verifies after the S13 lib bump —
   the recomputed canonical bytes hash to the signed hash;
2. a NEW-shape content (typed metadata present) signs and verifies.

``medical_kep`` src must not import ``report_models`` (leaf rules);
this is a test-only dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from medical_kep import MockProvider
from medical_kep.envelope import Envelope
from medical_kep.provider import DocumentDisplayMetadata
from medical_kep.trust_store import TrustStore
from medical_kep.verify import verify_envelope

from report_models import ReportContent, canonical_content_bytes

pytestmark = pytest.mark.asyncio

_PRE_S13_FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "report_models"
    / "tests"
    / "fixtures"
    / "canonical_pre_s13"
)


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


def _trust_store_for(test_ca_dir: Path) -> TrustStore:
    bundle = test_ca_dir / "test-ca-bundle.pem"
    bundle.write_bytes((test_ca_dir / "ca.cert.pem").read_bytes())
    return TrustStore.load_from_dir(test_ca_dir, include_test_ca=True)


async def _sign_hash(provider: MockProvider, doc_hash: bytes) -> bytes:
    init = await provider.initiate(
        document_pdf_hash=doc_hash,
        display=_display(),
        signer_hint=None,
        callback_url="http://localhost/cb",
    )
    body = json.dumps({"approved": True}).encode("utf-8")
    sig = hmac.new(b"mock-callback-key", body, hashlib.sha256).hexdigest()
    envelope = await provider.handle_callback(
        provider_session_id=init.provider_session_id,
        callback_body=body,
        callback_headers={"X-Mock-Signature": sig},
    )
    return envelope.signed_bytes


async def test_old_shape_envelope_still_verifies(tmp_path: Path) -> None:
    """Sign over the frozen pre-S13 canonical bytes; verify against the
    hash recomputed by the CURRENT lib. Byte drift would fail here."""
    provider = MockProvider(environment="development", test_ca_dir=tmp_path)
    trust = _trust_store_for(tmp_path)

    for input_path in sorted(_PRE_S13_FIXTURES.glob("*.input.json")):
        frozen = (
            input_path.parent / input_path.name.replace(".input.json", ".canonical.bin")
        ).read_bytes()
        signed = await _sign_hash(provider, hashlib.sha256(frozen).digest())

        recomputed = canonical_content_bytes(
            ReportContent.model_validate(json.loads(input_path.read_text("utf-8")))
        )
        result = verify_envelope(
            parsed=Envelope(signed).parse(),
            expected_document_hash=hashlib.sha256(recomputed).digest(),
            trust_store=trust,
        )
        assert result.valid is True, (input_path.name, result.errors)


async def test_new_shape_content_signs_and_verifies(tmp_path: Path) -> None:
    provider = MockProvider(environment="development", test_ca_dir=tmp_path)
    trust = _trust_store_for(tmp_path)

    content = ReportContent.model_validate(
        {
            "template_id": "70cd91de-82b0-48e5-81ce-dcc01e0a2297",
            "template_schema_version": 1,
            "sections": [
                {
                    "section_key": "smoking_status",
                    "text": "не палить",
                    "field_specific_metadata": {
                        "selected": "never",
                        "source": "manual",
                    },
                }
            ],
        }
    )
    b = canonical_content_bytes(content)
    signed = await _sign_hash(provider, hashlib.sha256(b).digest())
    result = verify_envelope(
        parsed=Envelope(signed).parse(),
        expected_document_hash=hashlib.sha256(canonical_content_bytes(content)).digest(),
        trust_store=trust,
    )
    assert result.valid is True, result.errors
