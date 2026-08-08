"""Connector registry contracts (S03 consumer)."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from uuid import UUID

from .common import ContractVersion, StrictModel
from .document import EvidenceTier, LicenseClass, SourceAuthority
from .webref import WebSourceRef


class ConnectorKind(StrEnum):
    local_corpus = "local_corpus"
    tenant_corpus = "tenant_corpus"
    pubmed = "pubmed"
    guideline_registry = "guideline_registry"
    web = "web"
    drug = "drug"


class ConnectorStatus(StrEnum):
    ok = "ok"
    degraded = "degraded"
    unavailable = "unavailable"


class ConnectorDescriptor(StrictModel):
    id: str
    kind: ConnectorKind
    display_name: str
    version: str
    enabled: bool = True
    # S03 additive: registry/isolation metadata (frozen EvidenceSource protocol).
    authority_default: SourceAuthority | None = None
    needs_egress: bool = False
    tenant_flaggable: bool = False
    timeout_ms: int = 600


class PassageScores(StrictModel):
    """Per-stage score retention (S03): every stage's contribution stays
    visible for provenance and eval ablations."""

    dense: float | None = None
    lexical: float | None = None
    fused: float | None = None
    rerank: float | None = None
    final: float | None = None


class EvidencePassage(StrictModel):
    """A retrieval result, whatever connector produced it."""

    id: str
    connector_id: str
    text: str
    section_path: str | None = None
    document_version_id: UUID | None = None
    web_ref: WebSourceRef | None = None
    evidence_tier: EvidenceTier | None = None
    source_authority: SourceAuthority | None = None
    published_at: date | None = None
    score: float | None = None
    # S03 additive: full provenance + staged scoring.
    chunk_id: UUID | None = None
    document_id: UUID | None = None
    source_kind: ConnectorKind | None = None
    char_start: int | None = None
    char_end: int | None = None
    scores: PassageScores | None = None
    license_class: LicenseClass | None = None
    retracted: bool | None = None


class ConnectorMeta(StrictModel):
    kind: ConnectorKind
    connector_id: str
    status: ConnectorStatus
    latency_ms: int
    count: int


class RetrieveResponse(StrictModel):
    """Public shape of POST /retrieve (S03). Deterministic for identical
    (query, snapshot_id, sources, filters, k, lexicon_version, model_pins)."""

    passages: list[EvidencePassage] = []
    connector_meta: list[ConnectorMeta] = []
    degraded: bool = False
    snapshot_id: UUID | None = None
    lexicon_version: str = "1.0"
    contract_version: ContractVersion = "1.0"
