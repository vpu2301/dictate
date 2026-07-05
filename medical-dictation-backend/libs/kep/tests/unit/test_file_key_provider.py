"""FileKeyProvider — inline qualified signing over a fake UAPKI backend.

The real DSTU library is Linux-only; these tests substitute the backend
(a test double, per repo rules) and fabricate a structurally-real CMS
envelope with the mock provider's test CA so the provider's self-check
pipeline (crypto verdict + structural parse) is exercised end-to-end.
The real UAPKI path is covered by ``test_uapki_integration.py``
(``RUN_UAPKI_INTEGRATION=1``, Linux).
"""

from __future__ import annotations

import hashlib

import pytest
from medical_kep import (
    FileKeyProvider,
    InlineCredentials,
    InvalidCredentialsError,
    ProviderName,
    ProviderTransientError,
    SignatureLevel,
    SignerIdentity,
)
from medical_kep.mock_provider import _default_test_ca_dir, _ensure_test_ca, _sign_with_test_ca
from medical_kep.uapki_backend import UapkiSignResult, UapkiVerifyResult

from secret import Secret

CANONICAL = b'{"canonical_version":"1.0","probe":"file_key"}'
CANONICAL_HASH = hashlib.sha256(CANONICAL).digest()


class FakeBackend:
    """Stands in for UapkiBackend; emits a real (test-CA) CMS."""

    def __init__(
        self,
        *,
        open_fails: bool = False,
        verify_status: str = "VALID",
        tsa_applied: bool = False,
    ) -> None:
        self.open_fails = open_fails
        self.verify_status = verify_status
        self.tsa_applied = tsa_applied
        self.sign_calls: list[dict] = []
        self.closed = False
        self._ca = _ensure_test_ca(_default_test_ca_dir())

    def sign_detached(self, *, container: bytes, password: str, data: bytes) -> UapkiSignResult:
        self.sign_calls.append(
            {"container": bytes(container), "password": password, "data": data}
        )
        if self.open_fails:
            raise InvalidCredentialsError("key container rejected (uapki code 1035 INVALID_MAC)")
        cms = _sign_with_test_ca(
            ca=self._ca,
            doc_hash=hashlib.sha256(data).digest(),
            signer_full_name="Лікар Файловий",
            signer_ipn="1234567890",
        )
        return UapkiSignResult(
            signature_der=cms,
            signer_cert_der=b"",
            sign_algo_oid="1.2.804.2.1.1.1.1.3.1.1",
            tsa_applied=self.tsa_applied,
        )

    def verify_detached(self, *, signature_der: bytes, data: bytes) -> UapkiVerifyResult:
        return UapkiVerifyResult(
            total_valid=self.verify_status == "VALID",
            signature_status=self.verify_status,
            message_digest_status=self.verify_status,
            raw={},
        )

    def health(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


def _credentials(container: bytes = b"\x30\x82fake-container", password: str = "pw") -> InlineCredentials:
    return InlineCredentials(key_container=container, key_password=Secret(password))


def _signer() -> SignerIdentity:
    return SignerIdentity(sub="0" * 36, display_name="Dr File Key", username="doc@x")


@pytest.mark.asyncio
async def test_happy_path_produces_qualified_envelope() -> None:
    backend = FakeBackend()
    p = FileKeyProvider(backend=backend)  # type: ignore[arg-type]
    env = await p.sign_inline(
        canonical_bytes=CANONICAL, credentials=_credentials(), signer=_signer()
    )
    assert env.provider is ProviderName.FILE_KEY
    assert env.signature_level is SignatureLevel.QUALIFIED
    # The DTO came from a structural parse of the real CMS bytes.
    assert env.parsed.signer_full_name == "Test Clinician Leaf"
    assert env.parsed.document_hash_sha256 == CANONICAL_HASH
    assert env.parsed.cert_chain_pem  # chain embedded
    assert env.signed_bytes.startswith(b"\x30")  # DER CMS
    # The backend received exactly the canonical bytes.
    assert backend.sign_calls[0]["data"] == CANONICAL


@pytest.mark.asyncio
async def test_bad_container_or_password_maps_to_invalid_credentials() -> None:
    p = FileKeyProvider(backend=FakeBackend(open_fails=True))  # type: ignore[arg-type]
    with pytest.raises(InvalidCredentialsError):
        await p.sign_inline(
            canonical_bytes=CANONICAL, credentials=_credentials(), signer=_signer()
        )


@pytest.mark.asyncio
async def test_self_check_failure_never_returns_an_envelope() -> None:
    """A provider must never hand back an envelope the verifier rejects."""
    p = FileKeyProvider(backend=FakeBackend(verify_status="INVALID"))  # type: ignore[arg-type]
    with pytest.raises(ProviderTransientError, match="self-check"):
        await p.sign_inline(
            canonical_bytes=CANONICAL, credentials=_credentials(), signer=_signer()
        )


@pytest.mark.asyncio
async def test_missing_container_rejected_before_backend() -> None:
    backend = FakeBackend()
    p = FileKeyProvider(backend=backend)  # type: ignore[arg-type]
    with pytest.raises(InvalidCredentialsError):
        await p.sign_inline(
            canonical_bytes=CANONICAL,
            credentials=InlineCredentials(key_password=Secret("pw")),
            signer=_signer(),
        )
    with pytest.raises(InvalidCredentialsError):
        await p.sign_inline(
            canonical_bytes=CANONICAL,
            credentials=InlineCredentials(key_container=b"x"),
            signer=_signer(),
        )
    assert backend.sign_calls == []


@pytest.mark.asyncio
async def test_tsa_applied_marks_cades_t() -> None:
    p = FileKeyProvider(backend=FakeBackend(tsa_applied=True))  # type: ignore[arg-type]
    env = await p.sign_inline(
        canonical_bytes=CANONICAL, credentials=_credentials(), signer=_signer()
    )
    assert env.parsed.format == "CAdES-T"
    assert env.parsed.tsa_token_present is True


@pytest.mark.asyncio
async def test_aclose_closes_backend() -> None:
    backend = FakeBackend()
    p = FileKeyProvider(backend=backend)  # type: ignore[arg-type]
    await p.aclose()
    assert backend.closed is True
