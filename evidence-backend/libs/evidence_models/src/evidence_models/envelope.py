"""AnswerEnvelope v1 (rule RC1) and Segment (rule RC2).

Two safety properties are enforced at the schema level, not in pipeline code:

- ET2: an `evidence` segment with zero citations does not validate — the
  unsafe path does not compile.
- An `insufficient_basis` envelope with zero evidence segments is explicitly
  valid (rule SC3: honesty is a first-class answer state).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import model_validator

from .common import ContractVersion, StrictModel
from .document import EvidenceTier, SourceAuthority
from .followup import FollowUp
from .webref import WebSourceRef

# Hard cap on the serialized envelope (rule: oversized answers are a defect,
# not a rendering problem).
MAX_ENVELOPE_BYTES = 256 * 1024


class AnswerStatus(StrEnum):
    ok = "ok"
    insufficient_basis = "insufficient_basis"
    deflected = "deflected"


class SegmentKind(StrEnum):
    """The six segment kinds of rule FE7/RC2. Final; changes go through v2 + ADR."""

    evidence = "evidence"
    patient_fact = "patient_fact"
    interpretation = "interpretation"
    uncertainty = "uncertainty"
    missing_info = "missing_info"
    next_step = "next_step"


class FlagSeverity(StrEnum):
    info = "info"
    warning = "warning"
    critical = "critical"


class CheckOutcome(StrEnum):
    passed = "passed"
    warning = "warning"
    failed = "failed"
    not_applicable = "not_applicable"


class SourceKind(StrEnum):
    corpus = "corpus"
    web = "web"
    drug = "drug"


class SourceRef(StrictModel):
    """An entry of the envelope's sources[] panel; citations point at these."""

    id: str
    kind: SourceKind
    title: str
    evidence_tier: EvidenceTier | None = None
    source_authority: SourceAuthority | None = None
    document_version_id: UUID | None = None
    web: WebSourceRef | None = None


class Citation(StrictModel):
    # id of a SourceRef in the same envelope.
    source_id: str
    # Chunk/passage id inside the source; None for whole-document citations.
    passage_id: str | None = None
    # Exact supporting span, for the claim -> passage interaction (rule SC1).
    quote: str | None = None


class Segment(StrictModel):
    id: str
    kind: SegmentKind
    text: str
    citations: list[Citation] = []
    # field_path values resolving into the PatientSnapshot (rule CS3b).
    patient_fact_refs: list[str] = []
    strength: EvidenceTier | None = None

    @model_validator(mode="after")
    def _et2_evidence_requires_citation(self) -> Self:
        if self.kind is SegmentKind.evidence and not self.citations:
            raise ValueError("ET2: an 'evidence' segment must carry at least one citation")
        return self


class Flag(StrictModel):
    # e.g. "requires_clinical_review", "red_flag", "superseded_source".
    code: str
    severity: FlagSeverity
    message: str
    # Acknowledgment-gated rendering (rule CS2).
    requires_ack: bool = False


class CheckResult(StrictModel):
    """Result of one deterministic check (rule CS1); computed by code, never a model."""

    rule_id: str
    engine: str
    outcome: CheckOutcome
    severity: FlagSeverity
    entities: list[str] = []
    db_version: str | None = None
    narrative: str | None = None


class AnswerEnvelope(StrictModel):
    answer_id: UUID
    status: AnswerStatus
    summary_segments: list[Segment] = []
    detail_segments: list[Segment] = []
    followups: list[FollowUp] = []
    checks: list[CheckResult] = []
    sources: list[SourceRef] = []
    # Reference to the persisted AnswerProvenance record (ET1).
    provenance_ref: str
    flags: list[Flag] = []
    contract_version: ContractVersion = "1.0"

    @model_validator(mode="after")
    def _citations_resolve_to_sources(self) -> Self:
        source_ids = {s.id for s in self.sources}
        for segment in [*self.summary_segments, *self.detail_segments]:
            for citation in segment.citations:
                if citation.source_id not in source_ids:
                    raise ValueError(
                        f"citation in segment {segment.id!r} references unknown "
                        f"source {citation.source_id!r}"
                    )
        return self

    @model_validator(mode="after")
    def _size_cap(self) -> Self:
        size = len(self.model_dump_json().encode("utf-8"))
        if size > MAX_ENVELOPE_BYTES:
            raise ValueError(
                f"envelope serializes to {size} bytes, exceeding the {MAX_ENVELOPE_BYTES}-byte cap"
            )
        return self
