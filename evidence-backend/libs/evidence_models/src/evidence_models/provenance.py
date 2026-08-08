"""AnswerProvenance (rule ET1) and per-stage traces (rule BE5).

The persisted record is append-only (DB grant + immutability trigger); this
contract is its wire shape.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from .common import ContractVersion, StrictModel
from .webref import WebSourceRef


class StageOutcome(StrEnum):
    ok = "ok"
    retried = "retried"
    failed = "failed"
    skipped = "skipped"


class StageTrace(StrictModel):
    stage: str
    started_at: datetime
    ended_at: datetime
    outcome: StageOutcome
    meta: dict[str, str] = {}


class AnswerProvenance(StrictModel):
    answer_id: UUID
    question_ref: str
    # ET1(2): the exact patient context used.
    snapshot_hash: str | None = None
    consumed_fields: list[str] = []
    # ET1(3): retrieved evidence.
    passage_ids: list[str] = []
    web_refs: list[WebSourceRef] = []
    connectors: list[str] = []
    corpus_snapshot_id: UUID | None = None
    # ET1(6): everything needed to reproduce the run.
    model_pins: dict[str, str] = {}
    prompt_versions: dict[str, str] = {}
    pipeline_version: str
    build_version: str
    created_at: datetime
    contract_version: ContractVersion = "1.0"
