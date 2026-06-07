"""Canonical-JSON-for-signing.

Sprint-09 commits to RFC 8785 JCS (ADR-0024). Reuses sprint-02's
``audit.canonical.canonicalize`` so we don't carry two implementations.

The shape is **the legal contract**. Any structural change requires:
1. A new ADR.
2. A new ``CANONICAL_VERSION`` value.
3. A migration that re-signs in the new shape only on next amendment
   (existing signed envelopes keep their original version forever — the
   bytes that were signed are immutable).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from audit.canonical import canonicalize

CANONICAL_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class CanonicalReportInput:
    """What sprint-08 (reports) needs to provide for canonicalisation.

    All fields are either primitives or pre-serialised strings — no
    asyncpg.Record, no Pydantic models. This makes the input boundary
    easy to test and prevents subtle drift across types.
    """

    tenant_id: str  # UUID as string
    tenant_legal_name: str
    report_id: str  # UUID as string
    report_code: str  # human REP-YYYY-NNNNN
    report_version_id: str  # UUID as string
    report_version_number: int
    title: str
    encounter_date: str | None  # ISO-8601 date
    primary_author_full_name: str
    primary_author_id: str  # UUID
    primary_author_role: str
    co_author_names: list[str]
    patient_id: str | None  # UUID or None
    patient_full_name_redacted: str | None
    icd10_codes: list[str]
    sections: list[dict[str, Any]]  # ordered; each {section_key, text, transcript_segment_ids}
    template_id: str
    template_schema_version: int
    finalized_at: str  # ISO-8601 datetime
    signed_at_intent: (
        str  # the timestamp we *intend* to sign; provider TSA replaces this in the cert chain
    )


def canonicalize_report(payload: CanonicalReportInput) -> tuple[bytes, str]:
    """Produce the canonical bytes that get signed and their hex digest."""
    obj = {
        "canonical_version": CANONICAL_VERSION,
        "tenant": {
            "id": payload.tenant_id,
            "legal_name": payload.tenant_legal_name,
        },
        "report": {
            "id": payload.report_id,
            "code": payload.report_code,
            "version_id": payload.report_version_id,
            "version_number": payload.report_version_number,
            "title": payload.title,
            "encounter_date": payload.encounter_date,
            "icd10_codes": sorted(set(payload.icd10_codes)),
            "template": {
                "id": payload.template_id,
                "schema_version": payload.template_schema_version,
            },
            "primary_author": {
                "id": payload.primary_author_id,
                "full_name": payload.primary_author_full_name,
                "role": payload.primary_author_role,
            },
            "co_author_names": payload.co_author_names,
            "patient": (
                {
                    "id": payload.patient_id,
                    "full_name_redacted": payload.patient_full_name_redacted,
                }
                if payload.patient_id is not None
                else None
            ),
            "sections": [
                {
                    "section_key": s["section_key"],
                    "text": s.get("text", ""),
                    "transcript_segment_ids": list(s.get("transcript_segment_ids", [])),
                }
                for s in payload.sections
            ],
        },
        "lifecycle": {
            "finalized_at": payload.finalized_at,
            "signed_at_intent": payload.signed_at_intent,
        },
    }
    raw = canonicalize(obj)
    return raw, hashlib.sha256(raw).hexdigest()


def canonical_hash_hex(canonical_bytes: bytes) -> str:
    return hashlib.sha256(canonical_bytes).hexdigest()
