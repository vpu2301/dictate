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
    canonicalize_report,
    canonical_hash_hex,
)
from medical_kep.envelope import (
    Envelope,
    EnvelopeFormat,
    EnvelopeParseError,
    ParsedEnvelope,
)
from medical_kep.health import ProviderHealth
from medical_kep.mock_provider import MockProvider
from medical_kep.provider import (
    DocumentDisplayMetadata,
    InvalidCallbackError,
    ProviderName,
    SignedEnvelope,
    SignerHint,
    SigningProvider,
    SigningSessionInit,
    SigningSessionStatus,
    VerificationResult,
)
from medical_kep.trust_store import TrustStore, TrustStoreError
from medical_kep.verify import VerificationError, verify_envelope

__all__ = [
    "CANONICAL_VERSION",
    "DocumentDisplayMetadata",
    "Envelope",
    "EnvelopeFormat",
    "EnvelopeParseError",
    "InvalidCallbackError",
    "MockProvider",
    "ParsedEnvelope",
    "ProviderHealth",
    "ProviderName",
    "SignedEnvelope",
    "SignerHint",
    "SigningProvider",
    "SigningSessionInit",
    "SigningSessionStatus",
    "TrustStore",
    "TrustStoreError",
    "VerificationError",
    "VerificationResult",
    "canonical_hash_hex",
    "canonicalize_report",
    "verify_envelope",
]
