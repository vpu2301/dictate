"""Sprint-09 ``medical_kep`` — KEP signing library.

Public API:

- :class:`SigningProvider` ABC — implemented by Дія, ІІТ, and the
  mock. Sprint-09 services use *only* this interface; concrete
  providers are wired via :func:`make_provider`.
- :func:`canonicalize_report` — the canonical JSON shape that gets
  signed. Reuses :mod:`audit.canonical` (RFC 8785 JCS).
- :class:`Envelope` parser (parses PAdES/CAdES bytes into the
  ``ParsedEnvelope`` dataclass).
- :class:`TrustStore` — loads PEM CA bundles + TSA roots.
- :func:`verify_envelope` — full envelope verification (cert chain
  + OCSP + TSA + document hash binding).
- :class:`MockProvider` — CI/test provider; refuses to instantiate
  on production.

ADR-0022 (PAdES), ADR-0023 (provider abstraction), ADR-0024 (JCS
canonical JSON).
"""

from medical_kep.canonicalize import (
    CANONICAL_VERSION,
    canonical_hash_hex,
    canonicalize_report,
)
from medical_kep.dev_password_provider import (
    DevPasswordProvider,
    PasswordCheckResult,
    PasswordVerifier,
)
from medical_kep.envelope import (
    Envelope,
    EnvelopeFormat,
    EnvelopeParseError,
    ParsedEnvelope,
)
from medical_kep.file_key_provider import FileKeyProvider
from medical_kep.health import ProviderHealth
from medical_kep.mock_provider import MockProvider
from medical_kep.provider import (
    AccountLockedError,
    DocumentDisplayMetadata,
    InlineCredentials,
    InlineSigner,
    InvalidCallbackError,
    InvalidCredentialsError,
    ParsedEnvelopeDTO,
    ProviderName,
    ProviderTransientError,
    SignatureLevel,
    SignedEnvelope,
    SignerHint,
    SignerIdentity,
    SigningProvider,
    SigningSessionInit,
    SigningSessionStatus,
    VerificationResult,
)
from medical_kep.trust_store import TrustStore, TrustStoreError
from medical_kep.uapki_backend import UapkiBackend, UapkiConfig, UapkiError
from medical_kep.verify import VerificationError, verify_envelope

__all__ = [
    "CANONICAL_VERSION",
    "AccountLockedError",
    "DevPasswordProvider",
    "DocumentDisplayMetadata",
    "Envelope",
    "EnvelopeFormat",
    "EnvelopeParseError",
    "FileKeyProvider",
    "InlineCredentials",
    "InlineSigner",
    "InvalidCallbackError",
    "InvalidCredentialsError",
    "MockProvider",
    "ParsedEnvelope",
    "ParsedEnvelopeDTO",
    "PasswordCheckResult",
    "PasswordVerifier",
    "ProviderHealth",
    "ProviderName",
    "ProviderTransientError",
    "SignatureLevel",
    "SignedEnvelope",
    "SignerHint",
    "SignerIdentity",
    "SigningProvider",
    "SigningSessionInit",
    "SigningSessionStatus",
    "TrustStore",
    "TrustStoreError",
    "UapkiBackend",
    "UapkiConfig",
    "UapkiError",
    "VerificationError",
    "VerificationResult",
    "canonical_hash_hex",
    "canonicalize_report",
    "verify_envelope",
]
