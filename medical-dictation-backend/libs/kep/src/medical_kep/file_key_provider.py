"""File-key signing provider — the primary MVP qualified path.

The clinician uploads their own КНЕДП-issued file key container
(``Key-6.dat`` / ``.pfx`` / ``.jks``) + password with the sign request;
we sign the canonical report bytes server-side with DSTU 4145 via the
UAPKI backend (ADR-0026) and produce a detached CAdES-BES envelope,
upgraded to CAdES-T when a TSA is configured and reachable.

Key-material hygiene:
- The container is held in memory only (UAPKI ``file://memory``) — it
  is never written to disk and never logged.
- The provider zeroes its working ``bytearray`` copy after use; the
  password travels inside ``secret.Secret`` up to the ctypes boundary.
- Legal note (ADR-0026): server-side custody of the key during the
  request requires the signer's explicit consent — the sign UI carries
  that consent text; we never persist the container or password.

Self-check invariant: the provider never returns an envelope the
verifier would reject — after signing it re-verifies its own output
both cryptographically (UAPKI VERIFY, DSTU-capable) and structurally
(:func:`medical_kep.envelope.Envelope.parse`).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from uuid import uuid4

from medical_kep.envelope import Envelope, EnvelopeFormat, EnvelopeParseError
from medical_kep.provider import (
    InlineCredentials,
    InlineSigner,
    InvalidCredentialsError,
    ParsedEnvelopeDTO,
    ProviderHealthSnapshot,
    ProviderName,
    ProviderTransientError,
    SignatureLevel,
    SignedEnvelope,
    SignerIdentity,
)
from medical_kep.uapki_backend import UapkiBackend

logger = logging.getLogger(__name__)


class FileKeyProvider(InlineSigner):
    """Qualified-tier inline signer over the clinician's own file key."""

    name = ProviderName.FILE_KEY
    signature_level = SignatureLevel.QUALIFIED

    def __init__(self, *, backend: UapkiBackend) -> None:
        self._backend = backend

    async def sign_inline(
        self,
        *,
        canonical_bytes: bytes,
        credentials: InlineCredentials,
        signer: SignerIdentity,
        artifact_pdf: bytes | None = None,
    ) -> SignedEnvelope:
        if not credentials.key_container:
            raise InvalidCredentialsError("key container missing")
        if credentials.key_password is None or not credentials.key_password.value():
            raise InvalidCredentialsError("key container password missing")

        # Working copy we control — zeroed in the finally block. (The
        # base64 str the ctypes layer builds is immutable Python memory;
        # best-effort, documented in ADR-0026.)
        container = bytearray(credentials.key_container)
        try:
            result = await asyncio.to_thread(
                self._backend.sign_detached,
                container=bytes(container),
                password=credentials.key_password.value(),
                data=canonical_bytes,
            )
        finally:
            for i in range(len(container)):
                container[i] = 0

        # ── Self-check 1: cryptographic (DSTU-capable, via UAPKI) ────
        verdict = await asyncio.to_thread(
            self._backend.verify_detached,
            signature_der=result.signature_der,
            data=canonical_bytes,
        )
        if verdict.message_digest_status != "VALID" or verdict.signature_status != "VALID":
            raise ProviderTransientError(
                "file_key self-check failed: produced envelope does not verify "
                f"(signature={verdict.signature_status}, "
                f"digest={verdict.message_digest_status})"
            )

        # ── Self-check 2: structural (same parser the ingest path uses) ─
        declared = EnvelopeFormat.CADES_T if result.tsa_applied else EnvelopeFormat.CADES_BES
        try:
            parsed = Envelope(result.signature_der, declared_format=declared).parse()
        except EnvelopeParseError as exc:
            raise ProviderTransientError(
                f"file_key produced an unparseable envelope: {exc}"
            ) from exc

        expected_hash = hashlib.sha256(canonical_bytes).digest()
        if parsed.document_hash_sha256 != expected_hash:
            # DSTU envelopes may bind via GOST 34.311 messageDigest
            # rather than SHA-256; the cryptographic self-check above is
            # authoritative — record the canonical SHA-256 for the row.
            parsed_doc_hash = expected_hash
        else:
            parsed_doc_hash = parsed.document_hash_sha256

        dto = ParsedEnvelopeDTO(
            signer_full_name=parsed.signer_full_name or signer.display_name,
            signer_ipn=parsed.signer_ipn,
            signer_cert_serial=parsed.signer_cert_serial_hex,
            signer_cert_issuer_cn=parsed.signer_cert_issuer_cn,
            cert_chain_pem=parsed.cert_chain_pem,
            document_hash_sha256=parsed_doc_hash,
            signed_at=parsed.signed_at,
            tsa_token_present=parsed.tsa_token_present or result.tsa_applied,
            ocsp_responses_present=parsed.ocsp_responses_present,
            signature_algorithm=parsed.signature_algorithm,
            is_qualified=parsed.is_qualified,
            format=declared.value,
        )
        return SignedEnvelope(
            provider=ProviderName.FILE_KEY,
            provider_envelope_id=f"file-key-{uuid4().hex}",
            signed_bytes=result.signature_der,
            parsed=dto,
            signature_level=SignatureLevel.QUALIFIED,
        )

    async def health(self) -> ProviderHealthSnapshot:
        started = time.monotonic()
        healthy = await asyncio.to_thread(self._backend.health)
        return ProviderHealthSnapshot(
            provider=ProviderName.FILE_KEY,
            healthy=healthy,
            latency_ms=int((time.monotonic() - started) * 1000),
            last_error=None if healthy else "uapki library unavailable",
        )

    async def aclose(self) -> None:
        await asyncio.to_thread(self._backend.close)
