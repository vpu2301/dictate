"""Metadata enrichment + canonical identity (spec D4, rules RR2/RR5).

Canonical id preference: DOI > PMID > PMCID > МОЗ order > ISBN > content
hash. metadata_overrides (operator/CLI-provided) win over parser-extracted
values; classification fields default conservatively — unknown license parks
as 'restricted' (excluded from snapshots, see constants)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from evidence_ingest.domain.parse_types import ParsedDocument

_VALID_AUTHORITIES = {"tenant", "national", "international", "primary_literature", "other"}
_VALID_TIERS = {"guideline", "systematic_review", "rct", "observational", "other"}
_VALID_LICENSES = {
    "public_domain",
    "open_license",
    "licensed_redistributable",
    "licensed_internal",
    "restricted",
}


@dataclass(frozen=True, slots=True)
class EnrichedMetadata:
    canonical_id: str
    title: str
    source_authority: str
    evidence_tier: str
    license_class: str
    license_known: bool
    jurisdiction: str | None
    specialty: list[str]
    published_at: str | None
    language: str | None


def canonical_id_for(doc: ParsedDocument, content: bytes) -> str:
    metadata = doc.metadata
    for key, prefix in (
        ("doi", "doi:"),
        ("pmid", "pmid:"),
        ("pmcid", "pmcid:"),
        ("moz_order", "moz:"),
        ("isbn", "isbn:"),
    ):
        value = metadata.get(key)
        if value:
            return prefix + value.strip().lower()
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _pick(overrides: dict[str, str], key: str, valid: set[str], default: str) -> str:
    value = overrides.get(key, "").strip().lower()
    return value if value in valid else default


def enrich(
    doc: ParsedDocument,
    content: bytes,
    overrides: dict[str, str] | None = None,
) -> EnrichedMetadata:
    overrides = overrides or {}
    license_value = overrides.get("license_class", "").strip().lower()
    license_known = license_value in _VALID_LICENSES
    specialty_raw = overrides.get("specialty", "")
    return EnrichedMetadata(
        canonical_id=overrides.get("canonical_id") or canonical_id_for(doc, content),
        title=overrides.get("title") or doc.title or "(untitled)",
        source_authority=_pick(overrides, "source_authority", _VALID_AUTHORITIES, "other"),
        evidence_tier=_pick(overrides, "evidence_tier", _VALID_TIERS, "other"),
        license_class=license_value if license_known else "restricted",
        license_known=license_known,
        jurisdiction=overrides.get("jurisdiction") or None,
        specialty=[s.strip() for s in specialty_raw.split(",") if s.strip()],
        published_at=overrides.get("published") or doc.metadata.get("published"),
        language=overrides.get("language") or doc.language,
    )


def content_checksum(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()
