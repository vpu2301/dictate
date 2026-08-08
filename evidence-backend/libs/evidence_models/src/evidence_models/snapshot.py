"""PatientSnapshot v1 (rule RC3; S05 consumer).

Every fact records where it came from (provenance), how trustworthy its
structure is (status), and whether it must pass de-identification before
reaching any model (deid_required, consumed by the S05 de-id gate).
deid_required defaults to True: the safe path is the default path.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from .common import Coding, ContractVersion, Quantity, StrictModel


class FactSource(StrEnum):
    chart = "chart"
    act = "act"
    user_reported = "user_reported"
    host_reported = "host_reported"


class FactStatus(StrEnum):
    coded = "coded"
    uncoded = "uncoded"
    conflict = "conflict"


class FactOriginKind(StrEnum):
    patient = "patient"
    encounter = "encounter"
    anamnesis = "anamnesis"
    note = "note"
    report = "report"
    consent = "consent"
    act = "act"
    user = "user"
    host = "host"


class FactProvenance(StrictModel):
    # Platform document id (note/report/act/...) the fact was extracted from.
    origin_ref: str
    origin_kind: FactOriginKind
    recorded_at: datetime | None = None


class PatientFact(StrictModel):
    id: str
    # Snapshot field path, e.g. "medications[0].dose" — the unit of
    # patient-fact grounding (rule CS3b) and of consumed_fields in provenance.
    field_path: str
    label: str
    value: str | None = None
    quantity: Quantity | None = None
    coding: list[Coding] = []
    provenance: FactProvenance
    source: FactSource
    status: FactStatus
    deid_required: bool = True


class PatientSnapshot(StrictModel):
    snapshot_id: str
    patient_ref: str
    encounter_ref: str | None = None
    taken_at: datetime
    facts: list[PatientFact] = []
    # Canonical hash over the ordered facts; referenced by AnswerProvenance (ET1).
    snapshot_hash: str
    contract_version: ContractVersion = "1.0"
