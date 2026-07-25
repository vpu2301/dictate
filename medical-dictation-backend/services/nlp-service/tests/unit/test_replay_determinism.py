"""Frozen-version replay (sprint 13, ADR-0028).

The sprint-05/07 contract is byte-equal replay **under a frozen
``pipeline_version``**. Sprint 13 inserted a stage, so the two versions
are different pipelines and are replayed as such:

- ``nlp-v1.0.0`` fixtures replay through the SIX-stage pipeline —
  proving a historical session processed before sprint 13 still
  produces exactly the bytes it produced then.
- ``nlp-v1.1.0`` fixtures replay through the SEVEN-stage pipeline.

Scope: the deterministic stages only. ``punctuation`` is an ML model
whose bytes are pinned by the model revision, not by this contract
(see docs/models/PINS.md), so it is excluded here exactly as the
sprint-07 eval harness excludes it.

Regenerate with::

    uv run --project services/nlp-service python \\
        services/nlp-service/tests/fixtures/replay/regenerate.py
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

from nlp_service.pipeline.base import (
    AbbreviationSnapshot,
    ChoiceOption,
    ProcessingContext,
    Stage,
    StageInput,
)
from nlp_service.pipeline.orchestrator import Orchestrator, _encode_for_cache
from nlp_service.stages import (
    AbbreviationStage,
    ConfidenceStage,
    DateNormStage,
    FieldExtractionStage,
    NumberNormStage,
)

pytestmark = pytest.mark.asyncio

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "replay"


def stages_for_version(version: str) -> list[Stage]:
    """The deterministic stage list as it stood at ``version``.

    This mapping is what makes historical replay meaningful: a v1.0.0
    session is replayed through the pipeline that produced it.
    """
    if version == "nlp-v1.0.0":
        return [NumberNormStage(), DateNormStage(), AbbreviationStage(), ConfidenceStage()]
    if version == "nlp-v1.1.0":
        return [
            NumberNormStage(),
            DateNormStage(),
            AbbreviationStage(),
            FieldExtractionStage(confidence_threshold=0.8),
            ConfidenceStage(),
        ]
    raise AssertionError(f"no stage list registered for pipeline_version {version!r}")


def ctx_from_fixture(doc: dict) -> ProcessingContext:
    c = doc["context"]
    return ProcessingContext(
        tenant_id=UUID(c["tenant_id"]),
        language=c["language"],
        specialty=c["specialty"],
        reference_date=date.fromisoformat(c["reference_date"]),
        is_partial=c["is_partial"],
        abbreviation_snapshot=AbbreviationSnapshot(entries=(), fingerprint=c["fingerprint"]),
        pipeline_version=doc["pipeline_version"],
        template_sections=tuple(
            __import__("nlp_service.pipeline.base", fromlist=["TemplateSection"]).TemplateSection(
                id=UUID(s["id"]),
                name=s["name"],
                aliases=tuple(s.get("aliases", ())),
                section_key=s.get("section_key", ""),
                field_type=s.get("field_type", "free_text"),
                options=tuple(
                    ChoiceOption(
                        value=o["value"], label=o["label"], aliases=tuple(o.get("aliases", ()))
                    )
                    for o in s.get("options", ())
                ),
            )
            for s in c.get("template_sections", ())
        ),
    )


def _fixtures() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


async def test_fixture_corpus_covers_both_versions() -> None:
    versions = {json.loads(p.read_text("utf-8"))["pipeline_version"] for p in _fixtures()}
    assert versions == {"nlp-v1.0.0", "nlp-v1.1.0"}, versions


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
async def test_replay_is_byte_equal(path: Path) -> None:
    doc = json.loads(path.read_text("utf-8"))
    orch = Orchestrator(stages=stages_for_version(doc["pipeline_version"]))
    out = await orch.run(ctx_from_fixture(doc), StageInput(text=doc["input"]["text"]))
    actual = _encode_for_cache(out).decode("utf-8")
    assert actual == doc["expected_encoded"], (
        f"{path.name}: replay under frozen {doc['pipeline_version']} drifted"
    )


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
async def test_replay_twice_is_stable(path: Path) -> None:
    doc = json.loads(path.read_text("utf-8"))
    ctx = ctx_from_fixture(doc)
    input = StageInput(text=doc["input"]["text"])
    a = await Orchestrator(stages=stages_for_version(doc["pipeline_version"])).run(ctx, input)
    b = await Orchestrator(stages=stages_for_version(doc["pipeline_version"])).run(ctx, input)
    assert _encode_for_cache(a) == _encode_for_cache(b)


async def test_v1_0_0_fixtures_carry_no_extraction_metadata() -> None:
    """The historical pipeline had no extractor; its replays must not
    grow one."""
    for path in _fixtures():
        doc = json.loads(path.read_text("utf-8"))
        if doc["pipeline_version"] == "nlp-v1.0.0":
            assert "field_extraction" not in doc["expected_encoded"], path.name
