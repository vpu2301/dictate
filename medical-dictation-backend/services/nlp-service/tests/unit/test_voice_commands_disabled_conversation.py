"""Conversation mode: voice-commands stage disabled end-to-end (sprint 14).

A conversation-mode transcript carries PATIENT speech — «новий абзац»
said by a patient must stay verbatim prose, never become an editing
operation. The orchestrator is driven with the REAL ``VoiceCommandStage``
once normally (operation fires) and once with
``stages_disabled=("voice_commands",)`` (text verbatim, no operations).
"""

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
from nlp_service.pipeline.orchestrator import Orchestrator
from nlp_service.stages.voice_command_matcher import CommandSpec
from nlp_service.stages.voice_commands import VoiceCommandStage


def _w(text: str, start: float, end: float, p: float = 0.95) -> Word:
    return Word(text=text, start_s=start, end_s=end, probability=p)


def _ctx(*, stages_disabled: tuple[str, ...] = ()) -> ProcessingContext:
    return ProcessingContext(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        language="uk",
        specialty=None,
        reference_date=date(2026, 1, 1),
        is_partial=False,
        abbreviation_snapshot=AbbreviationSnapshot(entries=(), fingerprint="x"),
        pipeline_version="t",
        stages_disabled=stages_disabled,
    )


def _orchestrator() -> Orchestrator:
    specs = [
        CommandSpec(
            intent="newparagraph",
            language="uk",
            phrases=(("новий", "абзац"),),
            requires_pause_before_ms=200,
            min_avg_probability=0.85,
        ),
    ]
    stage = VoiceCommandStage(specs_by_language={"uk": specs})
    return Orchestrator(stages=[stage])


_WORDS = (
    _w("скарги", 0.0, 0.5),
    _w("новий", 1.5, 1.8),
    _w("абзац", 1.9, 2.3),
    _w("висновок", 3.1, 3.6),
)
_TEXT = "скарги новий абзац висновок"


def test_voice_command_fires_when_stage_enabled() -> None:
    out = asyncio.run(_orchestrator().run(_ctx(), StageInput(text=_TEXT, words=_WORDS)))
    assert [o.op for o in out.operations] == ["insert_paragraph_break"]
    # Command tokens stripped from the text (streaming, non-inline path).
    assert "новий" not in out.text
    assert "абзац" not in out.text


def test_voice_command_inert_when_stage_disabled() -> None:
    out = asyncio.run(
        _orchestrator().run(
            _ctx(stages_disabled=("voice_commands",)),
            StageInput(text=_TEXT, words=_WORDS),
        )
    )
    # Patient speech stays verbatim; no editing operations.
    assert out.text == _TEXT
    assert out.operations == ()
    assert out.voice_commands == ()
    assert out.metadata["voice_commands.skipped_disabled"] is True
    # Words untouched — nothing flagged as a command token.
    assert all(not w.is_voice_command_token for w in out.words)
