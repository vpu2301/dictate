"""Mock signing provider used in CI + dev.

Production refusal: the constructor raises ``RuntimeError`` if the
environment is ``production`` so a misconfigured deployment can't
accidentally accept mock envelopes. The mock CA is committed at
``libs/kep/tests/fixtures/test-ca/``; production trust stores never
include it.

The mock generates real CMS envelopes signed by the test CA so the
verification path exercises the same code as Дія / ІІТ. The only
difference is the trust anchor.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from asn1crypto import cms as cms_asn1
from asn1crypto import x509
from cryptography import x509 as crypto_x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from medical_kep.envelope import EnvelopeFormat
from medical_kep.provider import (
    DocumentDisplayMetadata,
    InvalidCallbackError,
    ParsedEnvelopeDTO,
    ProviderHealthSnapshot,
    ProviderName,
    SignedEnvelope,
    SignerHint,
    SigningProvider,
    SigningSessionInit,
)

logger = logging.getLogger(__name__)


class MockProvider(SigningProvider):
    """In-process mock provider.

    Production refusal: instantiating this class with ``ENVIRONMENT
    == 'production'`` raises ``RuntimeError``. This is the load-bearing
    safety check; do not loosen it.
    """

    name = ProviderName.MOCK

    def __init__(
        self,
        *,
        environment: str | None = None,
        test_ca_dir: Path | None = None,
    ) -> None:
        # noqa justified: this is a library-level fail-safe, not app config —
        # callers pass `environment` from their typed settings; the env read is
        # only the fallback that refuses to construct a mock signer in prod.
        env = (environment or os.environ.get("ENVIRONMENT", "development")).lower()  # noqa: ENV001
        if env in ("production", "prod"):
            raise RuntimeError(
                "MockProvider may NOT run in production "
                f"(ENVIRONMENT={env!r}). Configuring it here is a "
                "production-bug; refusing to construct."
            )
        self._sessions: dict[str, dict[str, Any]] = {}
        self._ca = _ensure_test_ca(test_ca_dir or _default_test_ca_dir())

    # ── SigningProvider impl ────────────────────────────────────────

    async def initiate(
        self,
        *,
        document_pdf_hash: bytes,
        display: DocumentDisplayMetadata,
        signer_hint: SignerHint | None,
        callback_url: str,
    ) -> SigningSessionInit:
        sid = uuid4().hex
        self._sessions[sid] = {
            "doc_hash": document_pdf_hash,
            "display": display,
            "callback_url": callback_url,
            "signed": False,
        }
        url = f"http://localhost:9999/mock-sign/{sid}"
        return SigningSessionInit(
            provider=ProviderName.MOCK,
            provider_session_id=sid,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            redirect_url=url,
            qr_payload=f"mock://{sid}",
        )

    async def handle_callback(
        self,
        *,
        provider_session_id: str,
        callback_body: bytes,
        callback_headers: dict[str, str],
    ) -> SignedEnvelope:
        # The mock callback body is a tiny JSON dict
        # {"approved": true|false, "signer_full_name": "...", "signer_ipn": "..."}.
        try:
            payload = json.loads(callback_body)
        except Exception as exc:  # noqa: BLE001
            raise InvalidCallbackError(f"mock callback body not JSON: {exc}") from exc

        # Mock signature: HMAC over body with a static dev key.
        provided_sig = callback_headers.get("X-Mock-Signature", "")
        expected_sig = _hmac_hex(b"mock-callback-key", callback_body)
        if not _consteq(provided_sig, expected_sig):
            raise InvalidCallbackError("invalid mock callback signature")

        session = self._sessions.get(provider_session_id)
        if session is None:
            raise InvalidCallbackError("unknown mock session_id")
        if not payload.get("approved"):
            raise InvalidCallbackError("mock signing rejected by user")

        doc_hash: bytes = session["doc_hash"]
        signer_name = payload.get("signer_full_name") or "Лікар Тестовий Олексійович"
        signer_ipn = payload.get("signer_ipn") or "1234567890"

        envelope_bytes = _sign_with_test_ca(
            ca=self._ca,
            doc_hash=doc_hash,
            signer_full_name=signer_name,
            signer_ipn=signer_ipn,
        )
        parsed = ParsedEnvelopeDTO(
            signer_full_name=signer_name,
            signer_ipn=signer_ipn,
            signer_cert_serial=format(self._ca.leaf_serial, "x"),
            signer_cert_issuer_cn=self._ca.issuer_cn,
            cert_chain_pem=self._ca.chain_pem,
            document_hash_sha256=doc_hash,
            signed_at=datetime.now(UTC),
            tsa_token_present=False,
            ocsp_responses_present=False,
            signature_algorithm="sha256WithRSAEncryption",
            is_qualified=False,
            format=EnvelopeFormat.CADES_BES.value,
        )
        session["signed"] = True
        return SignedEnvelope(
            provider=ProviderName.MOCK,
            provider_envelope_id=f"mock-env-{provider_session_id}",
            signed_bytes=envelope_bytes,
            parsed=parsed,
        )

    async def health(self) -> ProviderHealthSnapshot:
        return ProviderHealthSnapshot(provider=ProviderName.MOCK, healthy=True, latency_ms=0)

    async def aclose(self) -> None:
        self._sessions.clear()


# ── Test CA scaffolding ─────────────────────────────────────────────


class _TestCA:
    def __init__(self, ca_cert, ca_key, leaf_cert, leaf_key) -> None:
        self.ca_cert = ca_cert
        self.ca_key = ca_key
        self.leaf_cert = leaf_cert
        self.leaf_key = leaf_key
        self.leaf_serial = leaf_cert.serial_number
        self.issuer_cn = next(
            (a.value for a in leaf_cert.issuer if a.oid == NameOID.COMMON_NAME),
            "Test CA",
        )
        self.chain_pem = [
            leaf_cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            ca_cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        ]


def _default_test_ca_dir() -> Path:
    # __file__ = libs/kep/src/medical_kep/mock_provider.py
    # parents[2] = libs/kep
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "test-ca"


def _ensure_test_ca(dir_path: Path) -> _TestCA:
    """Generate or load a deterministic-by-seed test CA + leaf cert.

    Files written into ``dir_path``:
    - ``ca.key.pem``, ``ca.cert.pem``
    - ``leaf.key.pem``, ``leaf.cert.pem``
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    ca_key_path = dir_path / "ca.key.pem"
    ca_cert_path = dir_path / "ca.cert.pem"
    leaf_key_path = dir_path / "leaf.key.pem"
    leaf_cert_path = dir_path / "leaf.cert.pem"

    if all(p.exists() for p in (ca_key_path, ca_cert_path, leaf_key_path, leaf_cert_path)):
        return _load_test_ca(dir_path)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = crypto_x509.Name(
        [
            crypto_x509.NameAttribute(NameOID.COUNTRY_NAME, "UA"),
            crypto_x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Medical Dictation Test CA"),
            crypto_x509.NameAttribute(NameOID.COMMON_NAME, "Medical Dictation Test CA"),
        ]
    )
    now = datetime.now(UTC)
    ca_cert = (
        crypto_x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(crypto_x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(crypto_x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            crypto_x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_subject = crypto_x509.Name(
        [
            crypto_x509.NameAttribute(NameOID.COUNTRY_NAME, "UA"),
            crypto_x509.NameAttribute(NameOID.COMMON_NAME, "Test Clinician Leaf"),
            crypto_x509.NameAttribute(NameOID.SERIAL_NUMBER, "1234567890"),
        ]
    )
    leaf_cert = (
        crypto_x509.CertificateBuilder()
        .subject_name(leaf_subject)
        .issuer_name(ca_subject)
        .public_key(leaf_key.public_key())
        .serial_number(crypto_x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=730))
        .add_extension(
            crypto_x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )

    ca_key_path.write_bytes(
        ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ca_cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    leaf_key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    leaf_cert_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    return _TestCA(ca_cert, ca_key, leaf_cert, leaf_key)


def _load_test_ca(dir_path: Path) -> _TestCA:
    ca_key = serialization.load_pem_private_key(
        (dir_path / "ca.key.pem").read_bytes(), password=None
    )
    ca_cert = crypto_x509.load_pem_x509_certificate((dir_path / "ca.cert.pem").read_bytes())
    leaf_key = serialization.load_pem_private_key(
        (dir_path / "leaf.key.pem").read_bytes(), password=None
    )
    leaf_cert = crypto_x509.load_pem_x509_certificate((dir_path / "leaf.cert.pem").read_bytes())
    return _TestCA(ca_cert, ca_key, leaf_cert, leaf_key)


# ── CMS SignedData assembly (CAdES-BES shape) ───────────────────────


def _sign_with_test_ca(
    *, ca: _TestCA, doc_hash: bytes, signer_full_name: str, signer_ipn: str
) -> bytes:
    """Produce a CMS SignedData with detached content + the embedded
    chain. Signed-attrs include messageDigest and signing-time."""
    leaf_der = ca.leaf_cert.public_bytes(serialization.Encoding.DER)
    ca_der = ca.ca_cert.public_bytes(serialization.Encoding.DER)

    leaf_x = x509.Certificate.load(leaf_der)
    ca_x = x509.Certificate.load(ca_der)

    signed_attrs = cms_asn1.CMSAttributes(
        [
            cms_asn1.CMSAttribute({"type": "content_type", "values": ["data"]}),
            cms_asn1.CMSAttribute({"type": "message_digest", "values": [doc_hash]}),
            cms_asn1.CMSAttribute(
                {
                    "type": "signing_time",
                    "values": [cms_asn1.Time({"utc_time": datetime.now(UTC)})],
                }
            ),
        ]
    )

    tbs = signed_attrs.dump()
    signature = ca.leaf_key.sign(tbs, padding.PKCS1v15(), hashes.SHA256())

    signer_info = cms_asn1.SignerInfo(
        {
            "version": "v1",
            "sid": cms_asn1.SignerIdentifier(
                "issuer_and_serial_number",
                cms_asn1.IssuerAndSerialNumber(
                    {
                        "issuer": leaf_x.issuer,
                        "serial_number": leaf_x.serial_number,
                    }
                ),
            ),
            "digest_algorithm": {"algorithm": "sha256"},
            "signed_attrs": signed_attrs,
            "signature_algorithm": {"algorithm": "sha256_rsa"},
            "signature": signature,
        }
    )

    signed_data = cms_asn1.SignedData(
        {
            "version": "v1",
            "digest_algorithms": [{"algorithm": "sha256"}],
            "encap_content_info": {
                "content_type": "data",
                # detached: content omitted
            },
            "certificates": [
                cms_asn1.CertificateChoices("certificate", leaf_x),
                cms_asn1.CertificateChoices("certificate", ca_x),
            ],
            "signer_infos": [signer_info],
        }
    )
    content_info = cms_asn1.ContentInfo(
        {
            "content_type": "signed_data",
            "content": signed_data,
        }
    )
    return content_info.dump()


# ── crypto helpers ──────────────────────────────────────────────────


def _hmac_hex(key: bytes, msg: bytes) -> str:
    import hmac

    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _consteq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))
