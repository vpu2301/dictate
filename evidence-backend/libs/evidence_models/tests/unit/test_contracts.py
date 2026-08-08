"""S01 verification protocol §9.1-9.2: round-trip + ET2 + size cap + integrity."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import jsonschema
import pytest
from pydantic import ValidationError

from evidence_models import (
    MAX_ENVELOPE_BYTES,
    AnswerEnvelope,
    AnswerProvenance,
    AnswerStatus,
    CheckOutcome,
    CheckResult,
    Citation,
    ClinicalConcept,
    ClinicalIntent,
    EvidenceTier,
    FactOriginKind,
    FactProvenance,
    FactSource,
    FactStatus,
    Flag,
    FlagSeverity,
    FollowUp,
    FollowUpAnswerType,
    FollowUpPriority,
    PatientFact,
    PatientSnapshot,
    QuestionType,
    Segment,
    SegmentKind,
    SourceKind,
    SourceRef,
    WebSourceRef,
    WebTrustTier,
)
from evidence_models.jsonschema_export import CONTRACTS, build_schema

CONTRACTS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent.parent
    / "docs"
    / "api"
    / "evidence-contracts"
)


def _source(source_id: str = "src-1") -> SourceRef:
    return SourceRef(
        id=source_id,
        kind=SourceKind.corpus,
        title="ESC Guideline 2024",
        evidence_tier=EvidenceTier.guideline,
    )


def _segment(kind: SegmentKind, **overrides: object) -> Segment:
    base: dict[str, object] = {
        "id": f"seg-{kind.value}",
        "kind": kind,
        "text": f"text for {kind.value}",
    }
    if kind is SegmentKind.evidence:
        base["citations"] = [Citation(source_id="src-1", passage_id="chunk-1", quote="...")]
        base["strength"] = EvidenceTier.guideline
    if kind is SegmentKind.patient_fact:
        base["patient_fact_refs"] = ["medications[0].dose"]
    base.update(overrides)
    return Segment.model_validate(base)


def _envelope_all_kinds() -> AnswerEnvelope:
    return AnswerEnvelope(
        answer_id=uuid4(),
        status=AnswerStatus.ok,
        summary_segments=[_segment(SegmentKind.evidence)],
        detail_segments=[_segment(k) for k in SegmentKind if k is not SegmentKind.evidence],
        followups=[
            FollowUp(
                id="fu-1",
                question="Current eGFR?",
                why_needed="Dose adjustment depends on renal function",
                priority=FollowUpPriority.high,
                answer_type=FollowUpAnswerType.quantity,
                unit="mL/min/1.73m2",
            )
        ],
        checks=[
            CheckResult(
                rule_id="ddi:apixaban+ketoconazole",
                engine="interactions",
                outcome=CheckOutcome.failed,
                severity=FlagSeverity.critical,
                entities=["apixaban", "ketoconazole"],
                db_version="ddi-2026.06",
            )
        ],
        sources=[_source()],
        provenance_ref="prov:00000000-0000-0000-0000-000000000001",
        flags=[
            Flag(
                code="requires_clinical_review",
                severity=FlagSeverity.warning,
                message="Therapy-change answer — clinical review required",
            )
        ],
    )


class TestRoundTrip:
    def test_envelope_all_six_kinds_round_trips(self) -> None:
        env = _envelope_all_kinds()
        raw = env.model_dump_json()
        again = AnswerEnvelope.model_validate_json(raw)
        assert again == env
        kinds = {s.kind for s in [*again.summary_segments, *again.detail_segments]}
        assert kinds == set(SegmentKind)

    def test_envelope_validates_against_exported_schema(self) -> None:
        env = _envelope_all_kinds()
        schema = json.loads((CONTRACTS_DIR / "answer_envelope.v1.schema.json").read_text())
        jsonschema.validate(json.loads(env.model_dump_json()), schema)

    def test_all_committed_schemas_match_models(self) -> None:
        for name, (model, version) in CONTRACTS.items():
            committed = json.loads(
                (CONTRACTS_DIR / f"{name}.v{version.split('.')[0]}.schema.json").read_text()
            )
            assert committed == build_schema(name, model, version), f"{name} schema drifted"

    def test_snapshot_and_intent_and_provenance_round_trip(self) -> None:
        snapshot = PatientSnapshot(
            snapshot_id="snap-1",
            patient_ref="patient:123",
            encounter_ref="encounter:9",
            taken_at=datetime.now(tz=UTC),
            facts=[
                PatientFact(
                    id="f1",
                    field_path="labs.egfr",
                    label="eGFR",
                    value="54",
                    provenance=FactProvenance(
                        origin_ref="report:42", origin_kind=FactOriginKind.report
                    ),
                    source=FactSource.chart,
                    status=FactStatus.coded,
                )
            ],
            snapshot_hash="sha256:abc",
        )
        assert PatientSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
        assert snapshot.facts[0].deid_required is True  # safe default

        intent = ClinicalIntent(
            question_type=QuestionType.dosing,
            concepts=[ClinicalConcept(text="apixaban")],
            negations=["no active bleeding"],
        )
        assert ClinicalIntent.model_validate_json(intent.model_dump_json()) == intent

        prov = AnswerProvenance(
            answer_id=uuid4(),
            question_ref="question:1",
            snapshot_hash="sha256:abc",
            consumed_fields=["labs.egfr"],
            passage_ids=["chunk-1"],
            web_refs=[
                WebSourceRef(
                    url="https://www.escardio.org/guideline",
                    domain="escardio.org",
                    trust_tier=WebTrustTier.professional_society,
                    accessed_at=datetime.now(tz=UTC),
                )
            ],
            connectors=["local_corpus", "web"],
            pipeline_version="p1",
            build_version="dev",
            created_at=datetime.now(tz=UTC),
        )
        assert AnswerProvenance.model_validate_json(prov.model_dump_json()) == prov


class TestET2:
    def test_evidence_segment_without_citation_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ET2"):
            Segment(id="s1", kind=SegmentKind.evidence, text="unsupported claim")

    def test_evidence_segment_with_citation_accepted(self) -> None:
        seg = _segment(SegmentKind.evidence)
        assert seg.citations

    def test_non_evidence_kinds_need_no_citation(self) -> None:
        for kind in SegmentKind:
            if kind is SegmentKind.evidence:
                continue
            assert Segment(id="s", kind=kind, text="t").citations == []

    def test_insufficient_basis_with_zero_evidence_segments_is_valid(self) -> None:
        env = AnswerEnvelope(
            answer_id=uuid4(),
            status=AnswerStatus.insufficient_basis,
            detail_segments=[_segment(SegmentKind.missing_info)],
            provenance_ref="prov:x",
        )
        assert env.status is AnswerStatus.insufficient_basis


class TestEnvelopeIntegrity:
    def test_citation_must_reference_a_source_in_the_envelope(self) -> None:
        with pytest.raises(ValidationError, match="unknown source"):
            AnswerEnvelope(
                answer_id=uuid4(),
                status=AnswerStatus.ok,
                summary_segments=[_segment(SegmentKind.evidence)],
                sources=[],  # src-1 missing
                provenance_ref="prov:x",
            )

    def test_oversized_envelope_rejected(self) -> None:
        big_text = "x" * (MAX_ENVELOPE_BYTES + 1)
        with pytest.raises(ValidationError, match="cap"):
            AnswerEnvelope(
                answer_id=uuid4(),
                status=AnswerStatus.ok,
                detail_segments=[_segment(SegmentKind.interpretation, text=big_text)],
                provenance_ref="prov:x",
            )

    def test_extra_fields_forbidden_everywhere(self) -> None:
        with pytest.raises(ValidationError):
            AnswerEnvelope.model_validate(
                {
                    "answer_id": str(uuid4()),
                    "status": "ok",
                    "provenance_ref": "p",
                    "smuggled": 1,
                }
            )
        with pytest.raises(ValidationError):
            Segment.model_validate(
                {"id": "s", "kind": "interpretation", "text": "t", "smuggled": 1}
            )
