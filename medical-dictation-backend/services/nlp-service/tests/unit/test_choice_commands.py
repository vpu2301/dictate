"""Option-command resolution + the four new Operations (sprint 13, step 07).

Voice selection writes structured clinical data directly, so the FSM
layer resolves option names **exactly** — no fuzziness. A near-miss
must never become a selection, and prose must never become a command.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest

from nlp_service.pipeline.base import (
    AbbreviationSnapshot,
    ChoiceOption,
    CommandSlot,
    ProcessingContext,
    StageInput,
    TemplateSection,
    Word,
)
from nlp_service.pipeline.orchestrator import Orchestrator
from nlp_service.stages.field_extraction import FieldExtractionStage
from nlp_service.stages.operations import _TABLE, KNOWN_INTENTS, operations_for
from nlp_service.stages.voice_command_matcher import CommandSpec, VoiceCommandMatcher
from nlp_service.stages.voice_commands import VoiceCommandStage

_SMOKING = TemplateSection(
    id=uuid4(),
    name="Статус куріння",
    aliases=("куріння",),
    section_key="smoking_status",
    field_type="choice",
    options=(
        ChoiceOption(value="never", label="не палить", aliases=("не палить", "не курить")),
        ChoiceOption(value="current", label="палить", aliases=("палить", "курить")),
    ),
)
_ALLERGIES = TemplateSection(
    id=uuid4(),
    name="Алергії",
    aliases=("алергії",),
    section_key="allergies",
    field_type="multi_choice",
    options=(
        ChoiceOption(value="penicillin", label="пеніцилін", aliases=("пеніцилін",)),
        ChoiceOption(value="latex", label="латекс", aliases=("латекс",)),
    ),
)
_FREE_TEXT = TemplateSection(
    id=uuid4(), name="Скарги", aliases=("скарги",), section_key="complaints", field_type="free_text"
)

_SPECS = [
    CommandSpec(
        intent="choice.set",
        language="uk",
        phrases=(("обрати",), ("вибрати",), ("встановити",)),
        requires_pause_before_ms=250,
        is_option_command=True,
        exact_match_only=True,
    ),
    CommandSpec(
        intent="choice.add",
        language="uk",
        phrases=(("додати",),),
        requires_pause_before_ms=250,
        is_option_command=True,
        exact_match_only=True,
    ),
    CommandSpec(
        intent="choice.remove",
        language="uk",
        phrases=(("прибрати",), ("видалити",)),
        requires_pause_before_ms=250,
        is_option_command=True,
        exact_match_only=True,
    ),
    CommandSpec(
        intent="diagnosis.capture",
        language="uk",
        phrases=(("діагноз",),),
        requires_pause_before_ms=300,
        min_avg_probability=0.88,
        exact_match_only=True,
    ),
]


def _words(tokens: list[str], *, lead_ms: int = 500, p: float = 0.95) -> list[Word]:
    out: list[Word] = []
    t = lead_ms / 1000.0
    for token in tokens:
        out.append(Word(text=token, start_s=t, end_s=t + 0.30, probability=p))
        t += 0.32
    return out


def _matcher(sections: tuple[TemplateSection, ...] = (_SMOKING, _ALLERGIES)) -> VoiceCommandMatcher:
    return VoiceCommandMatcher(_SPECS, language="uk", template_sections=sections)


def _one(tokens: list[str], sections: tuple[TemplateSection, ...] = (_SMOKING, _ALLERGIES)):
    results = _matcher(sections).detect(_words(tokens))
    return results[0] if results else None


# ── the VERIFY case ─────────────────────────────────────────────────


def test_obraty_kuryt_resolves_to_set_choice() -> None:
    """«обрати курить» → set_choice with the option VALUE (slug)."""
    match = _one(["обрати", "курить"])
    assert match is not None
    assert match.slot.intent == "choice.set"
    assert match.slot.arg == {"section_key": "smoking_status", "value": "current"}
    op = operations_for(match.slot)
    assert op.op == "set_choice"
    assert op.arg == {"section_key": "smoking_status", "value": "current"}


def test_multi_word_option_resolves() -> None:
    match = _one(["обрати", "не", "палить"])
    assert match is not None
    assert match.slot.arg == {"section_key": "smoking_status", "value": "never"}


def test_label_and_alias_both_resolve() -> None:
    by_label = _one(["обрати", "палить"])
    by_alias = _one(["обрати", "курить"])
    assert by_label is not None and by_alias is not None
    assert (
        by_label.slot.arg
        == by_alias.slot.arg
        == {
            "section_key": "smoking_status",
            "value": "current",
        }
    )


def test_the_spoken_words_are_consumed_not_left_in_prose() -> None:
    match = _one(["обрати", "не", "палить"])
    assert match is not None
    assert match.consumed_word_indices == (0, 1, 2)


# ── add / remove on multi_choice ────────────────────────────────────


def test_add_resolves_on_multi_choice() -> None:
    match = _one(["додати", "пеніцилін"])
    assert match is not None
    assert match.slot.intent == "choice.add"
    assert match.slot.arg == {"section_key": "allergies", "value": "penicillin"}
    assert operations_for(match.slot).op == "add_choice"


def test_remove_resolves_on_multi_choice() -> None:
    match = _one(["прибрати", "латекс"])
    assert match is not None
    assert match.slot.intent == "choice.remove"
    assert operations_for(match.slot).op == "remove_choice"


def test_set_on_multi_choice_is_replace_all() -> None:
    """Documented judgment call: `set_choice` on a multi-select replaces
    the whole selection. Predictability over cleverness."""
    match = _one(["обрати", "латекс"])
    assert match is not None
    assert match.slot.intent == "choice.set"
    assert match.slot.arg == {"section_key": "allergies", "value": "latex"}


# ── strict semantics: the no-op reasons ─────────────────────────────


@pytest.mark.parametrize("head", ["додати", "прибрати"])
def test_add_remove_on_single_choice_no_ops_with_a_reason(head: str) -> None:
    """add/remove are meaningless on a single-select field — they must
    NOT be silently reinterpreted as `set`."""
    match = _one([head, "курить"])
    assert match is not None
    assert match.slot.arg is not None
    assert match.slot.arg["reason"] == "not_a_multi_choice_section"
    op = operations_for(match.slot)
    assert op.op == "unknown_intent"
    assert op.arg is not None and op.arg["reason"] == "not_a_multi_choice_section"


def test_ambiguous_option_across_sections_no_ops() -> None:
    """The same words naming options in two sections: never pick one."""
    dup = TemplateSection(
        id=uuid4(),
        name="Інше",
        section_key="other",
        field_type="multi_choice",
        options=(
            ChoiceOption(value="latex", label="латекс", aliases=("латекс",)),
            ChoiceOption(value="x", label="інше", aliases=("інше",)),
        ),
    )
    match = _one(["обрати", "латекс"], sections=(_ALLERGIES, dup))
    assert match is not None
    assert match.slot.arg is not None
    assert match.slot.arg["reason"] == "option_ambiguous"
    assert operations_for(match.slot).op == "unknown_intent"


# ── prose safety: rejection, not a prose-eating no-op ───────────────


def test_unknown_option_leaves_the_words_as_prose() -> None:
    """A no-op would still CONSUME the head, deleting a clinical word.
    "встановити діагноз поки неможливо" must stay untouched prose."""
    assert _one(["встановити", "діагноз", "поки", "неможливо"]) is None


def test_command_head_with_no_choice_sections_is_prose() -> None:
    assert _one(["обрати", "курить"], sections=(_FREE_TEXT,)) is None


def test_command_head_alone_is_prose() -> None:
    assert _one(["обрати"]) is None


def test_near_miss_never_fires_the_opposite_action() -> None:
    """THE clinical-safety case: "прибрати" (remove) is Levenshtein-2
    from "обрати" (set). Fuzzy heads would turn "remove penicillin" into
    "select penicillin" — the opposite clinical statement."""
    match = _one(["прибрати", "пеніцилін"])
    assert match is not None
    assert match.slot.intent == "choice.remove", "a remove must never become a set"


def test_misspelled_head_does_not_fire() -> None:
    assert _one(["обраті", "курить"]) is None


def test_misspelled_option_does_not_fire() -> None:
    """No fuzzy option matching at the command layer — a wrong slug is a
    wrong clinical fact."""
    assert _one(["обрати", "куритть"]) is None


# ── diagnosis.capture is a hint, never a selection ──────────────────


def test_diagnosis_capture_marks_where_the_text_begins() -> None:
    match = _one(["діагноз", "гіпертонічна", "хвороба"])
    assert match is not None
    assert match.slot.intent == "diagnosis.capture"
    assert match.slot.arg == {"from_word_index": "1"}
    op = operations_for(match.slot)
    assert op.op == "mark_diagnosis_text"


def test_diagnosis_capture_carries_no_code() -> None:
    """No ICD-10 is ever selected by voice (sprint-13 scope)."""
    match = _one(["діагноз", "гіпертонічна", "хвороба"])
    assert match is not None
    assert match.slot.arg is not None
    assert "code" not in match.slot.arg
    assert "value" not in match.slot.arg


# ── the 1:1 intent↔operation mapping ────────────────────────────────


def test_new_intents_are_in_the_table() -> None:
    for intent in ("choice.set", "choice.add", "choice.remove", "diagnosis.capture"):
        assert intent in _TABLE, intent
        assert intent in KNOWN_INTENTS, intent


def test_new_operations_are_distinct() -> None:
    ops = [_TABLE[i][0] for i in ("choice.set", "choice.add", "choice.remove", "diagnosis.capture")]
    assert ops == ["set_choice", "add_choice", "remove_choice", "mark_diagnosis_text"]
    assert len(set(ops)) == 4


def test_seeded_intents_all_have_operations() -> None:
    """Every intent the catalogue can emit must map to an operation."""
    import json
    from pathlib import Path

    seed_dir = Path(__file__).resolve().parents[4] / "infra" / "postgres" / "seed"
    for language in ("uk", "en"):
        for cmd in json.loads((seed_dir / f"voice_commands_{language}.json").read_text("utf-8")):
            intent = cmd["intent"]
            if intent.startswith("section."):
                continue
            assert intent in _TABLE, f"{language}: {intent} has no operation"


def test_unresolved_arg_becomes_unknown_intent_not_a_selection() -> None:
    slot = CommandSlot(
        intent="choice.set",
        span_start_s=0.0,
        span_end_s=0.3,
        confidence=0.95,
        arg={"reason": "option_ambiguous"},
    )
    op = operations_for(slot)
    assert op.op == "unknown_intent"
    assert op.arg == {"intent": "choice.set", "reason": "option_ambiguous"}


# ── ordering consequence: commands are stripped before extraction ───


@pytest.mark.asyncio
async def test_command_utterance_does_not_also_trigger_extraction() -> None:
    """Stage 1 strips command tokens from the text, so stage 6's
    extractor never sees the command phrase — one utterance must not both
    select via voice AND extract from the same words."""
    ctx = ProcessingContext(
        tenant_id=UUID("00000000-0000-0000-0000-00000000000a"),
        language="uk",
        specialty=None,
        reference_date=date(2026, 7, 23),
        is_partial=False,
        abbreviation_snapshot=AbbreviationSnapshot(entries=(), fingerprint="fp"),
        pipeline_version="nlp-v1.1.0",
        template_sections=(_SMOKING,),
    )
    orch = Orchestrator(
        stages=[
            VoiceCommandStage(specs_by_language={"uk": _SPECS}),
            FieldExtractionStage(confidence_threshold=0.8),
        ]
    )
    out = await orch.run(ctx, StageInput(text="обрати курить", words=_words(["обрати", "курить"])))

    ops = [o.op for o in out.operations]
    assert ops == ["set_choice"], ops
    # The command's words are gone from the text, so the extractor found
    # nothing to propose — no duplicate write from one utterance.
    assert out.text.strip() == ""
    assert "field_extraction.fields" not in out.metadata


@pytest.mark.asyncio
async def test_prose_still_extracts_normally() -> None:
    """The converse: ordinary dictation with no command still extracts."""
    ctx = ProcessingContext(
        tenant_id=UUID("00000000-0000-0000-0000-00000000000a"),
        language="uk",
        specialty=None,
        reference_date=date(2026, 7, 23),
        is_partial=False,
        abbreviation_snapshot=AbbreviationSnapshot(entries=(), fingerprint="fp"),
        pipeline_version="nlp-v1.1.0",
        template_sections=(_SMOKING,),
    )
    orch = Orchestrator(
        stages=[
            VoiceCommandStage(specs_by_language={"uk": _SPECS}),
            FieldExtractionStage(confidence_threshold=0.8),
        ]
    )
    out = await orch.run(
        ctx,
        StageInput(text="пацієнт курить", words=_words(["пацієнт", "курить"])),
    )
    assert out.operations == ()
    assert out.metadata["field_extraction.fields"]["smoking_status"]["selected"] == "current"


# ── the two seed sources must not drift ─────────────────────────────


def test_json_fixtures_and_migration_0055_agree() -> None:
    """As-built: the JSON fixtures are authoritative (the seeder DELETEs
    per language before re-inserting), while migration 0055 seeds the
    same rows for migration-only environments. If they drift, running
    the seeder would silently change behaviour."""
    import json
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[4]
    sql = (
        repo / "infra" / "postgres" / "migrations" / "0055_seed_anamnesis_voice_commands.sql"
    ).read_text("utf-8")

    for language in ("uk", "en"):
        fixtures = {
            cmd["intent"]: cmd
            for cmd in json.loads(
                (
                    repo / "infra" / "postgres" / "seed" / f"voice_commands_{language}.json"
                ).read_text("utf-8")
            )
            if cmd["intent"] in _S13
        }
        assert set(fixtures) == _S13, f"{language} fixtures missing S13 commands"
        for intent, cmd in fixtures.items():
            # The migration must list the same phrases for this intent.
            pattern = rf"\('{re.escape(intent)}', '{language}',\s*'(\[.*?\])'"
            found = re.search(pattern, sql, re.DOTALL)
            assert found, f"migration 0055 has no {intent}/{language} row"
            assert json.loads(found.group(1)) == cmd["phrases"], (
                f"{intent}/{language}: migration phrases differ from the JSON fixture"
            )


_S13 = {"choice.set", "choice.add", "choice.remove", "diagnosis.capture"}
