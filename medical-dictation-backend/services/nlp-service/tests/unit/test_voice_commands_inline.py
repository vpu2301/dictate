"""Stage-1 inline op application (batch path, ``apply_operations_inline``).

Streaming keeps ops editor-side; the batch path has no editor, so
text-shaped ops (dictated punctuation, line breaks) are applied straight
into ``text`` at the command's position. Non-inline behaviour (strip
only) must stay unchanged.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from nlp_service.pipeline.base import (
    AbbreviationSnapshot,
    ProcessingContext,
    StageInput,
    Word,
)
from nlp_service.stages.voice_command_matcher import CommandSpec
from nlp_service.stages.voice_commands import VoiceCommandStage


def _w(text: str, start: float, end: float, p: float = 0.95) -> Word:
    return Word(text=text, start_s=start, end_s=end, probability=p)


def _ctx(*, inline: bool) -> ProcessingContext:
    return ProcessingContext(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        language="uk",
        specialty=None,
        reference_date=date(2026, 1, 1),
        is_partial=False,
        abbreviation_snapshot=AbbreviationSnapshot(entries=(), fingerprint="x"),
        pipeline_version="t",
        apply_operations_inline=inline,
    )


def _stage() -> VoiceCommandStage:
    specs = [
        CommandSpec(
            intent="period",
            language="uk",
            phrases=(("крапка",),),
            requires_pause_before_ms=200,
            min_avg_probability=0.85,
        ),
        CommandSpec(
            intent="newparagraph",
            language="uk",
            phrases=(("новий", "абзац"),),
            requires_pause_before_ms=200,
            min_avg_probability=0.85,
        ),
    ]
    return VoiceCommandStage(specs_by_language={"uk": specs})


async def test_inline_trailing_period_applied() -> None:
    words = (_w("тижнів", 0.0, 0.4), _w("крапка", 1.0, 1.4))
    out = await _stage().process(
        _ctx(inline=True),
        StageInput(text="тижнів крапка", words=words),
    )
    assert out.text == "тижнів."
    # Ops are still reported for inspectability.
    assert [o.op for o in out.operations] == ["insert_punctuation"]
    assert out.words[1].is_voice_command_token


async def test_inline_midsegment_period_lands_midtext() -> None:
    words = (
        _w("патології", 0.0, 0.5),
        _w("крапка", 1.0, 1.4),
        _w("висновок", 2.2, 2.7),
    )
    out = await _stage().process(
        _ctx(inline=True),
        StageInput(text="патології крапка висновок", words=words),
    )
    assert out.text == "патології. висновок"


async def test_inline_leading_period_renders_bare_mark() -> None:
    # Whisper often puts a dictated «Крапка» in its own segment; the
    # bare mark lets the batch caller merge it into the previous one.
    out = await _stage().process(
        _ctx(inline=True),
        StageInput(text="крапка", words=(_w("крапка", 0.0, 0.4),)),
    )
    assert out.text == "."


async def test_inline_paragraph_break() -> None:
    words = (
        _w("збережені", 0.0, 0.5),
        _w("новий", 1.0, 1.3),
        _w("абзац", 1.4, 1.8),
        _w("висновок", 2.6, 3.1),
    )
    out = await _stage().process(
        _ctx(inline=True),
        StageInput(text="збережені новий абзац висновок", words=words),
    )
    assert out.text == "збережені\n\nвисновок"


async def test_non_inline_still_strips_only() -> None:
    words = (_w("тижнів", 0.0, 0.4), _w("крапка", 1.0, 1.4))
    out = await _stage().process(
        _ctx(inline=False),
        StageInput(text="тижнів крапка", words=words),
    )
    assert out.text == "тижнів"
    assert [o.op for o in out.operations] == ["insert_punctuation"]


async def test_inline_low_confidence_command_left_verbatim() -> None:
    # Below the probability gate the token is prose, not a command —
    # inline mode must not touch it.
    words = (_w("тижнів", 0.0, 0.4), _w("крапка", 1.0, 1.4, p=0.5))
    out = await _stage().process(
        _ctx(inline=True),
        StageInput(text="тижнів крапка", words=words),
    )
    assert out.text == "тижнів крапка"
    assert out.operations == ()
