"""Operations dispatch tests."""

from __future__ import annotations

from nlp_service.pipeline.base import CommandSlot
from nlp_service.stages.operations import operations_for


def test_newparagraph_to_paragraph_break() -> None:
    slot = CommandSlot(intent="newparagraph", span_start_s=0.0, span_end_s=0.5, confidence=0.95)
    op = operations_for(slot)
    assert op.op == "insert_paragraph_break"


def test_period_carries_value() -> None:
    slot = CommandSlot(intent="period", span_start_s=0.0, span_end_s=0.5, confidence=0.95)
    op = operations_for(slot)
    assert op.op == "insert_punctuation"
    assert op.arg == {"value": "."}


def test_section_passes_arg_through() -> None:
    slot = CommandSlot(
        intent="section.diagnosis",
        span_start_s=0.0,
        span_end_s=0.5,
        confidence=0.95,
        arg={"section_id": "abc"},
    )
    op = operations_for(slot)
    assert op.op == "navigate_section"
    assert op.arg == {"section_id": "abc"}


def test_unknown_intent_returns_marker() -> None:
    slot = CommandSlot(
        intent="not_in_table", span_start_s=0.0, span_end_s=0.5, confidence=0.95
    )
    op = operations_for(slot)
    assert op.op == "unknown_intent"
    assert op.arg == {"intent": "not_in_table"}


def test_save_draft_no_arg() -> None:
    slot = CommandSlot(intent="save_draft", span_start_s=0.0, span_end_s=0.5, confidence=0.95)
    op = operations_for(slot)
    assert op.op == "save_draft"
    assert op.arg is None
