"""Canonical consent document v1 (S11 step 03).

The RFC 8785 (JCS) canonicalisation of this document is what the clinician
signs for a ``method='digital'`` consent. Field set is LOCKED for version
"1" — any change is a new ``CONSENT_CANONICAL_VERSION`` with its own
verification story (mirror of the report canonical's contract, ADR-0024).

The document embeds the sha256 of the approved consent-text file, so the
signature binds the exact wording (see ``consent_texts.py``), and snapshots
the patient/clinician display names at capture time: if those rows drift
before signing, recomputation no longer matches ``patient_consents.
canonical_hash`` and the sign attempt is rejected — re-capture the consent.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID

from audit.canonical import canonicalize

CONSENT_CANONICAL_VERSION = "1"


def canonical_consent(
    *,
    consent_id: UUID,
    tenant_id: UUID,
    patient_id: UUID,
    patient_name_uk: str,
    consent_type: str,
    text_version: str,
    text_sha256: bytes,
    granted_at: datetime,
    attested_by_sub: UUID,
    attested_by_name: str,
    language: str = "uk",
) -> tuple[dict, bytes, str]:
    """Build the canonical document → ``(doc, jcs_bytes, sha256_hex)``."""
    doc = {
        "canonical_version": CONSENT_CANONICAL_VERSION,
        "kind": "patient_consent",
        "consent_id": str(consent_id),
        "tenant_id": str(tenant_id),
        "patient_id": str(patient_id),
        "patient_name_uk": patient_name_uk,
        "consent_type": consent_type,
        "text_version": text_version,
        "text_sha256_hex": text_sha256.hex(),
        "granted_at": granted_at.isoformat(),
        "attested_by_sub": str(attested_by_sub),
        "attested_by_name": attested_by_name,
        "language": language,
    }
    jcs = canonicalize(doc)
    return doc, jcs, hashlib.sha256(jcs).hexdigest()
