"""Confidence-span computation tests."""

from __future__ import annotations

import asyncio
from datetime import date
from uuid import UUID

from nlp_service.pipeline.base import (
    AbbreviationSnapshot,
    ProcessingContext,
    StageInput,
    Word,
)
from nlp_service.stages.confidence import ConfidenceStage


def _ctx() -> ProcessingContext:
    return ProcessingContext(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        language="uk",
        specialty=None,
        reference_date=date(2026, 1, 1),
        is_partial=False,
        abbreviation_snapshot=AbbreviationSnapshot(entries=(), fingerprint="x"),
        pipeline_version="t",
    )


def test_high_concern_level() -> None:
    stage = ConfidenceStage()
    words = (Word(text="amoxicillin", start_s=0.0, end_s=0.5, probability=0.3),)
    out = asyncio.run(stage.process(_ctx(), StageInput(text="amoxicillin", words=words)))
    assert len(out.confidence_spans) == 1
    assert out.confidence_spans[0].level == "high_concern"
    assert out.confidence_spans[0].start_char == 0
    assert out.confidence_spans[0].end_char == len("amoxicillin")


def test_moderate_level() -> None:
    stage = ConfidenceStage()
    words = (Word(text="paracetamol", start_s=0.0, end_s=0.5, probability=0.5),)
    out = asyncio.run(stage.process(_ctx(), StageInput(text="paracetamol", words=words)))
    assert out.confidence_spans[0].level == "moderate"


def test_high_confidence_no_span() -> None:
    stage = ConfidenceStage()
    words = (Word(text="aspirin", start_s=0.0, end_s=0.5, probability=0.95),)
    out = asyncio.run(stage.process(_ctx(), StageInput(text="aspirin", words=words)))
    assert out.confidence_spans == ()


def test_adjacent_same_level_merged() -> None:
    stage = ConfidenceStage()
    words = (
        Word(text="foo", start_s=0.0, end_s=0.2, probability=0.3),
        Word(text="bar", start_s=0.21, end_s=0.4, probability=0.3),
    )
    out = asyncio.run(stage.process(_ctx(), StageInput(text="foo bar", words=words)))
    assert len(out.confidence_spans) == 1
    assert out.confidence_spans[0].start_char == 0
    assert out.confidence_spans[0].end_char == len("foo bar")


def test_short_word_matches_on_boundary_not_inside_another_word() -> None:
    # A low-confidence one-letter word ("і") must not be located inside the
    # earlier high-confidence word "діагноз" — that would paint the cue over
    # the wrong region. The whole-word match places it on the standalone "і".
    stage = ConfidenceStage()
    words = (
        Word(text="діагноз", start_s=0.0, end_s=0.3, probability=0.95),  # confident → no span
        Word(text="і", start_s=0.31, end_s=0.35, probability=0.3),  # low → one span
    )
    text = "діагноз і"
    out = asyncio.run(stage.process(_ctx(), StageInput(text=text, words=words)))
    assert len(out.confidence_spans) == 1
    span = out.confidence_spans[0]
    assert text[span.start_char : span.end_char] == "і"
    assert span.start_char == text.index(" і") + 1


def test_voice_command_token_skipped() -> None:
    stage = ConfidenceStage()
    words = (
        Word(text="новий", start_s=0.0, end_s=0.2, probability=0.3, is_voice_command_token=True),
    )
    out = asyncio.run(stage.process(_ctx(), StageInput(text="новий", words=words)))
    assert out.confidence_spans == ()
