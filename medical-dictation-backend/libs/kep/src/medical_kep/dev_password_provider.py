"""Development-only account-password signing scaffold.

``dev_password`` exists so the signing pipeline can be exercised
end-to-end during development without KEP key material. It produces
``signature_level='dev'`` envelopes: canonical hash + signer identity,
NO cryptographic envelope. A dev envelope can never verify as qualified
— the DB CHECK (`signed_envelopes_dev_never_qualified_check`), the
verifier short-circuit, and the PDF watermark all report the tier
truthfully.

Production guards (three independent layers — do not loosen any):
1. This constructor raises ``RuntimeError`` when ``ENVIRONMENT`` is
   production (mirrors :class:`MockProvider`).
2. ``signing_service.config`` REJECTS ``SIGNING_DEV_PASSWORD_ENABLED``
   when ``ENVIRONMENT=production`` (pydantic model validator), and the
   registry only wires this provider when the flag is on.
3. The CI gate ``check-no-dev-signing-in-prod-config`` fails the build
   if any production config/compose enables the flag.

Password verification is delegated to an injected async verifier that
the service wires to the SAME Keycloak password-grant call login uses —
we never store or compare password hashes ourselves, and Keycloak's
brute-force lockout applies unchanged.

Removal before launch = flip the flag off + delete this file + drop the
registry wiring (documented in todo.md).
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from medical_kep.provider import (
    AccountLockedError,
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

logger = logging.getLogger(__name__)

DEV_SIGNATURE_ALGORITHM = "none-dev-password"
DEV_ENVELOPE_FORMAT = "dev"


class PasswordCheckResult(StrEnum):
    OK = "ok"
    WRONG_PASSWORD = "wrong_password"
    LOCKED = "locked"
    UNAVAILABLE = "unavailable"


PasswordVerifier = Callable[[str, str], Awaitable[PasswordCheckResult]]
"""(username, password) → result, backed by the IdP password grant."""


class DevPasswordProvider(InlineSigner):
    """Dev-tier inline signer. Refuses to construct in production."""

    name = ProviderName.DEV_PASSWORD
    signature_level = SignatureLevel.DEV

    def __init__(
        self,
        *,
        password_verifier: PasswordVerifier,
        environment: str | None = None,
    ) -> None:
        # noqa justified: library-level fail-safe, not app config — callers
        # pass `environment` from their typed settings; the env read is only
        # the fallback that refuses to construct a dev signer in prod.
        env = (environment or os.environ.get("ENVIRONMENT", "development")).lower()  # noqa: ENV001
        if env in ("production", "prod"):
            raise RuntimeError(
                "DevPasswordProvider may NOT run in production "
                f"(ENVIRONMENT={env!r}). Configuring it here is a "
                "production bug; refusing to construct."
            )
        self._verify_password = password_verifier

    async def sign_inline(
        self,
        *,
        canonical_bytes: bytes,
        credentials: InlineCredentials,
        signer: SignerIdentity,
        artifact_pdf: bytes | None = None,
    ) -> SignedEnvelope:
        if credentials.dev_password is None or not credentials.dev_password.value():
            raise InvalidCredentialsError("dev_password missing")
        if not signer.username:
            raise InvalidCredentialsError("signer has no username for password re-auth")

        outcome = await self._verify_password(signer.username, credentials.dev_password.value())
        if outcome is PasswordCheckResult.WRONG_PASSWORD:
            raise InvalidCredentialsError("account password rejected")
        if outcome is PasswordCheckResult.LOCKED:
            raise AccountLockedError("account temporarily locked by the identity provider")
        if outcome is PasswordCheckResult.UNAVAILABLE:
            raise ProviderTransientError("identity provider unreachable for password re-auth")

        doc_hash = hashlib.sha256(canonical_bytes).digest()
        # The persisted bytes are the watermarked dev artifact PDF (a
        # visible, obviously-non-legal document); absent one, the
        # canonical JSON itself. Never a cryptographic envelope.
        signed_bytes = artifact_pdf if artifact_pdf is not None else canonical_bytes
        parsed = ParsedEnvelopeDTO(
            signer_full_name=signer.display_name,
            signer_ipn=None,
            signer_cert_serial="",
            signer_cert_issuer_cn="",
            cert_chain_pem=[],
            document_hash_sha256=doc_hash,
            signed_at=datetime.now(UTC),
            tsa_token_present=False,
            ocsp_responses_present=False,
            signature_algorithm=DEV_SIGNATURE_ALGORITHM,
            is_qualified=False,
            format=DEV_ENVELOPE_FORMAT,
        )
        return SignedEnvelope(
            provider=ProviderName.DEV_PASSWORD,
            provider_envelope_id=f"dev-{uuid4().hex}",
            signed_bytes=signed_bytes,
            parsed=parsed,
            signature_level=SignatureLevel.DEV,
        )

    async def health(self) -> ProviderHealthSnapshot:
        return ProviderHealthSnapshot(
            provider=ProviderName.DEV_PASSWORD, healthy=True, latency_ms=0
        )

    async def aclose(self) -> None:
        # Close the injected verifier's transport if it owns one
        # (KeycloakPasswordVerifier holds an httpx.AsyncClient).
        closer = getattr(self._verify_password, "aclose", None)
        if closer is not None:
            await closer()
