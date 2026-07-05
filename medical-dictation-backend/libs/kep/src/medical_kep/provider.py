"""SigningProvider ABC + DTOs.

Every concrete provider (Дія, ІІТ, mock) implements the same surface.
Services only depend on the ABC; the concrete provider is wired via
``make_provider`` at boot. This is the contract ADR-0023 commits to.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from secret import Secret


class ProviderName(StrEnum):
    DIIA = "diia"
    IIT = "iit"
    MOCK = "mock"
    FILE_KEY = "file_key"
    DEV_PASSWORD = "dev_password"


class SignatureLevel(StrEnum):
    """The legal tier of an envelope — immutable once persisted.

    ``QUALIFIED`` means the envelope carries a real CMS/PAdES
    cryptographic envelope purporting to be a КЕП; whether it *verifies*
    as qualified is decided by the trust store, never by this value
    alone. ``DEV`` is the development-only password-confirmation
    scaffold: no cryptographic envelope exists and the tier can never be
    reported as qualified on any surface.
    """

    QUALIFIED = "qualified"
    DEV = "dev"


class SigningSessionStatus(StrEnum):
    INITIATING = "initiating"
    AWAITING_USER = "awaiting_user"
    VERIFYING = "verifying"
    SIGNED = "signed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


# ── DTOs ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SignerHint:
    """Optional caller-provided hints about the expected signer.

    Used by the provider to pre-fill fields in their signing UI; never
    used to bypass the post-sign verification step.
    """

    full_name: str | None = None
    ipn_hmac_hex: str | None = None
    expected_cert_subject_cn: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentDisplayMetadata:
    """Human-facing metadata shown on the Дія / ІІТ signing page."""

    title: str
    report_code: str
    issuer_name: str
    encounter_date_iso: str | None
    page_count: int
    sha256_hex: str
    language: str = "uk"


@dataclass(frozen=True, slots=True)
class SigningSessionInit:
    """What the provider returns from ``initiate``.

    ``redirect_url`` is mobile-flow (Дія); ``local_helper_payload`` is
    smart-card-helper-flow (ІІТ). Exactly one of them is non-None.
    """

    provider: ProviderName
    provider_session_id: str
    expires_at: datetime
    redirect_url: str | None = None
    qr_payload: str | None = None
    local_helper_payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SignedEnvelope:
    """What we persist after a callback successfully arrives.

    ``signed_bytes`` is the raw PAdES/CAdES envelope (for the dev tier:
    the watermarked artifact PDF — there is no cryptographic envelope).
    ``parsed`` is the structural breakdown used by ``verify_envelope``
    (cert chain, signer info, TSA, OCSP, document hash binding).
    """

    provider: ProviderName
    provider_envelope_id: str
    signed_bytes: bytes
    parsed: ParsedEnvelopeDTO
    signature_level: SignatureLevel = SignatureLevel.QUALIFIED


@dataclass(frozen=True, slots=True)
class ParsedEnvelopeDTO:
    """Decoupled DTO for use across service boundaries (no asn1crypto types)."""

    signer_full_name: str | None
    signer_ipn: str | None  # plaintext only at parse time; HMAC immediately on persist
    signer_cert_serial: str
    signer_cert_issuer_cn: str
    cert_chain_pem: list[str]
    document_hash_sha256: bytes
    signed_at: datetime
    tsa_token_present: bool
    ocsp_responses_present: bool
    signature_algorithm: str
    is_qualified: bool
    format: str  # 'PAdES-LTV' / 'CAdES-T' / etc.


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Verifier output.

    Public ``/verify/{token}`` projects this into the JSON response.
    ``errors`` is exhaustive when ``valid=False``; the field is empty
    on success.
    """

    valid: bool
    errors: list[str] = field(default_factory=list)
    signer_full_name: str | None = None
    signer_cert_serial: str | None = None
    signer_cert_issuer_cn: str | None = None
    signed_at: datetime | None = None
    is_qualified: bool = False
    document_hash_sha256_hex: str | None = None
    format: str | None = None


# ── Exceptions ──────────────────────────────────────────────────────


class InvalidCallbackError(Exception):
    """Raised when a callback fails signature / HMAC verification."""


class ProviderTransientError(Exception):
    """The provider is currently unreachable / 5xx. Caller should
    retry or fall back to the other provider."""


class InvalidCredentialsError(Exception):
    """Inline signing credentials rejected — wrong key-container
    password, unreadable container, or wrong account password. Maps to
    400 (file_key) / 401 (dev_password) at the route layer."""


class AccountLockedError(Exception):
    """The identity provider reports the account as locked out
    (brute-force protection). Maps to 423 at the route layer."""


# ── ABC ─────────────────────────────────────────────────────────────


class SigningProvider(abc.ABC):
    """All providers expose the same five methods."""

    name: ProviderName

    @abc.abstractmethod
    async def initiate(
        self,
        *,
        document_pdf_hash: bytes,
        display: DocumentDisplayMetadata,
        signer_hint: SignerHint | None,
        callback_url: str,
    ) -> SigningSessionInit:
        """Open a signing session at the provider; return the redirect /
        helper payload the FE needs to drive the user through signing.
        """

    @abc.abstractmethod
    async def handle_callback(
        self,
        *,
        provider_session_id: str,
        callback_body: bytes,
        callback_headers: dict[str, str],
    ) -> SignedEnvelope:
        """Verify the callback signature, fetch the envelope (if not
        embedded in the callback), and return the structural parse.

        Raises :class:`InvalidCallbackError` on signature failure.
        """

    @abc.abstractmethod
    async def health(self) -> ProviderHealthSnapshot:
        """Lightweight probe of provider reachability."""

    @abc.abstractmethod
    async def aclose(self) -> None:
        """Release underlying resources (httpx clients, etc.)."""


@dataclass(frozen=True, slots=True)
class ProviderHealthSnapshot:
    provider: ProviderName
    healthy: bool
    latency_ms: int
    last_error: str | None = None


# ── Inline signing (file_key / dev_password) ────────────────────────


@dataclass(frozen=True, slots=True)
class SignerIdentity:
    """Who is signing — taken from the *verified JWT claims*, never from
    the request body."""

    sub: str  # UUID as string
    display_name: str
    username: str | None = None  # preferred_username/email; dev re-auth needs it


@dataclass(frozen=True, slots=True)
class InlineCredentials:
    """Secrets for an inline signing request. Passwords are wrapped in
    :class:`secret.Secret` so they can never leak via repr/str/JSON;
    the raw key container is bytes that MUST stay in memory only —
    inline signers zero their working copies after use and never write
    them to disk or logs.
    """

    key_container: bytes | None = None
    key_password: Secret[str] | None = None
    dev_password: Secret[str] | None = None


class InlineSigner(abc.ABC):
    """A provider that signs synchronously inside the request, with no
    redirect and no callback (``file_key``, ``dev_password``).

    Deliberately NOT a :class:`SigningProvider` subclass: the
    session-flow methods (``initiate``/``handle_callback``) have no
    meaningful implementation for an inline signer, and a stub that
    raises would poison the shared registry contract.
    """

    name: ProviderName
    signature_level: SignatureLevel

    @abc.abstractmethod
    async def sign_inline(
        self,
        *,
        canonical_bytes: bytes,
        credentials: InlineCredentials,
        signer: SignerIdentity,
        artifact_pdf: bytes | None = None,
    ) -> SignedEnvelope:
        """Sign ``canonical_bytes`` and return the envelope to persist.

        Raises :class:`InvalidCredentialsError` on a bad container /
        wrong password, :class:`AccountLockedError` on IdP lockout, and
        :class:`ProviderTransientError` when a dependency (TSA, IdP) is
        unreachable.
        """

    @abc.abstractmethod
    async def health(self) -> ProviderHealthSnapshot: ...

    @abc.abstractmethod
    async def aclose(self) -> None: ...
