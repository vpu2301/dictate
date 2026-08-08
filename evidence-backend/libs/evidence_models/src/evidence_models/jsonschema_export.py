"""JSON Schema export for every released contract.

Writes docs/api/evidence-contracts/<name>.v<major>.schema.json. The committed
files are the released surface: `make contracts-check` fails on drift (rule
E11 transposed to contracts) and scripts/contracts/lint_additive.py fails on
a breaking change without a major-version bump.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from .common import CONTRACTS_V1
from .connector import ConnectorDescriptor, EvidencePassage, RetrieveResponse
from .document import Chunk, CorpusSnapshot, Document, DocumentVersion
from .envelope import AnswerEnvelope, Segment
from .followup import FollowUp
from .intent import ClinicalIntent
from .provenance import AnswerProvenance, StageTrace
from .snapshot import PatientSnapshot
from .stream import AnswerStreamEvent
from .triage import TriageDecision
from .webref import WebSourceRef

# name -> (model, version). Nested models arrive via $defs; Segment, FollowUp,
# StageTrace and WebSourceRef are additionally exported standalone because
# they are contracts in their own right (rules RC2/RC3).
CONTRACTS: dict[str, tuple[type[BaseModel], str]] = {
    "answer_envelope": (AnswerEnvelope, CONTRACTS_V1),
    "segment": (Segment, CONTRACTS_V1),
    "patient_snapshot": (PatientSnapshot, CONTRACTS_V1),
    "clinical_intent": (ClinicalIntent, CONTRACTS_V1),
    "followup": (FollowUp, CONTRACTS_V1),
    "answer_provenance": (AnswerProvenance, CONTRACTS_V1),
    "answer_stream_event": (AnswerStreamEvent, CONTRACTS_V1),
    "triage_decision": (TriageDecision, CONTRACTS_V1),
    "stage_trace": (StageTrace, CONTRACTS_V1),
    "web_source_ref": (WebSourceRef, CONTRACTS_V1),
    "document": (Document, CONTRACTS_V1),
    "document_version": (DocumentVersion, CONTRACTS_V1),
    "chunk": (Chunk, CONTRACTS_V1),
    "corpus_snapshot": (CorpusSnapshot, CONTRACTS_V1),
    "connector_descriptor": (ConnectorDescriptor, CONTRACTS_V1),
    "evidence_passage": (EvidencePassage, CONTRACTS_V1),
    "retrieve_response": (RetrieveResponse, CONTRACTS_V1),
}


def schema_filename(name: str, version: str) -> str:
    major = version.split(".")[0]
    return f"{name}.v{major}.schema.json"


def build_schema(name: str, model: type[BaseModel], version: str) -> dict[str, object]:
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://evidentia.contracts/{name}@{version}"
    schema["x-contract-name"] = name
    schema["x-contract-version"] = version
    return schema


def export_all(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, (model, version) in sorted(CONTRACTS.items()):
        path = out_dir / schema_filename(name, version)
        schema = build_schema(name, model, version)
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/api/evidence-contracts")
    for path in export_all(out_dir):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
