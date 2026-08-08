"""Corpus document contracts (S02 consumer).

tenant_id is deliberately absent from wire models: tenancy is server-derived
(rule PD3) and lives only in the database rows.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from .common import StrictModel


class SourceAuthority(StrEnum):
    """Authority precedence per rule RR3 (tenant > national > international > primary literature)."""

    tenant = "tenant"
    national = "national"
    international = "international"
    primary_literature = "primary_literature"
    other = "other"


class EvidenceTier(StrEnum):
    """Fixed evidence-strength scale per rule SC2."""

    guideline = "guideline"
    systematic_review = "systematic_review"
    rct = "rct"
    observational = "observational"
    other = "other"


class LicenseClass(StrEnum):
    public_domain = "public_domain"
    open_license = "open_license"
    licensed_redistributable = "licensed_redistributable"
    licensed_internal = "licensed_internal"
    restricted = "restricted"


class Document(StrictModel):
    id: UUID
    # DOI / PMID / registry id used for dedup (rule RR5).
    canonical_id: str
    title: str
    source_authority: SourceAuthority
    evidence_tier: EvidenceTier
    jurisdiction: str | None = None
    specialty: list[str] = []
    published_at: date | None = None
    valid_until: date | None = None
    license_class: LicenseClass
    retracted: bool = False


class DocumentVersion(StrictModel):
    id: UUID
    document_id: UUID
    version: int
    # Object keys (EncryptedObjectStore) of the raw and parsed artifacts.
    content_ref: str
    parsed_ref: str | None = None
    checksum: str


class Chunk(StrictModel):
    id: UUID
    document_version_id: UUID
    section_path: str | None = None
    char_start: int
    char_end: int
    text: str


class CorpusSnapshot(StrictModel):
    id: UUID
    label: str
    member_version_ids: list[UUID]
    frozen_at: datetime
