"""Spoken-punctuation normalization tests (no model required).

Covers the two languages, spacing/capitalization, the conservative
guards for ambiguous words, and regression cases proving that normal
prose is left untouched.
"""

from __future__ import annotations

import asyncio
from datetime import date
from uuid import UUID

import pytest

from nlp_service.pipeline.base import (
    AbbreviationSnapshot,
    ProcessingContext,
    StageInput,
)
from nlp_service.pipeline.orchestrator import Orchestrator
from nlp_service.stages import (
    AbbreviationStage,
    ConfidenceStage,
    DateNormStage,
    NumberNormStage,
    PunctuationStage,
    VoiceCommandStage,
)
from nlp_service.stages.spoken_punctuation import normalize_spoken_punctuation
from nlp_service.stages.spoken_punctuation_stage import SpokenPunctuationStage

# ── The canonical examples from the ASR spec ────────────────────────


def test_ukrainian_clinical_example() -> None:
    src = (
        "Пацієнт скаржиться на біль у грудях крапка задишки немає кома "
        "сатурація нормальна крапка"
    )
    expected = (
        "Пацієнт скаржиться на біль у грудях. Задишки немає, "
        "сатурація нормальна."
    )
    assert normalize_spoken_punctuation(src, "uk") == expected


def test_english_clinical_example() -> None:
    src = (
        "The patient denies chest pain period oxygen saturation is normal "
        "comma no pleural effusion period"
    )
    expected = (
        "The patient denies chest pain. Oxygen saturation is normal, "
        "no pleural effusion."
    )
    assert normalize_spoken_punctuation(src, "en") == expected


# ── Per-command coverage ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("біль крапка", "Біль."),
        ("біль кома задишка", "Біль, задишка"),
        (
            "температура нормальна двокрапка тридцять шість",
            "Температура нормальна: тридцять шість",
        ),
        ("діагноз крапка з комою план", "Діагноз; план"),
        ("все добре знак оклику", "Все добре!"),
        ("це нормально знак питання", "Це нормально?"),
        ("постав крапку тут", "Постав. Тут"),
    ],
)
def test_uk_single_commands(src: str, expected: str) -> None:
    assert normalize_spoken_punctuation(src, "uk") == expected


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("pain period", "Pain."),
        ("pain full stop", "Pain."),
        ("pain comma nausea", "Pain, nausea"),
        ("history colon none", "History: none"),
        ("first semicolon second", "First; second"),
        ("really exclamation mark", "Really!"),
        ("really exclamation point", "Really!"),
        ("is it normal question mark", "Is it normal?"),
    ],
)
def test_en_single_commands(src: str, expected: str) -> None:
    assert normalize_spoken_punctuation(src, "en") == expected


# ── Newlines ────────────────────────────────────────────────────────


def test_uk_new_line_and_paragraph() -> None:
    src = "перший рядок новий рядок другий рядок новий абзац третій"
    out = normalize_spoken_punctuation(src, "uk")
    assert out == "Перший рядок\nДругий рядок\n\nТретій"


def test_en_new_line_and_paragraph() -> None:
    src = "line one new line line two new paragraph line three"
    out = normalize_spoken_punctuation(src, "en")
    assert out == "Line one\nLine two\n\nLine three"


def test_newlines_do_not_create_duplicate_spaces() -> None:
    out = normalize_spoken_punctuation("a new line b", "en")
    assert "  " not in out
    assert " \n" not in out and "\n " not in out


# ── Spacing / capitalization contract ───────────────────────────────


def test_no_space_before_mark_one_space_after() -> None:
    out = normalize_spoken_punctuation("pain period nausea period", "en")
    assert out == "Pain. Nausea."
    assert " ." not in out


def test_comma_does_not_capitalize_next_word() -> None:
    out = normalize_spoken_punctuation("pain comma nausea", "en")
    assert out == "Pain, nausea"


def test_terminator_capitalizes_next_word() -> None:
    out = normalize_spoken_punctuation("pain period nausea", "en")
    assert out == "Pain. Nausea"


# ── Conservative guards — normal prose must NOT be corrupted ─────────


def test_питання_as_noun_is_preserved_mid_sentence() -> None:
    # "питання" here is the common noun "question", not a command.
    src = "це важливе питання про діагноз"
    assert normalize_spoken_punctuation(src, "uk") == "Це важливе питання про діагноз"


def test_питання_trailing_is_treated_as_question_mark() -> None:
    src = "чи все нормально питання"
    assert normalize_spoken_punctuation(src, "uk") == "Чи все нормально?"


def test_знак_as_content_word_is_preserved() -> None:
    # "знак Бабінського" (Babinski sign) must survive — only the fixed
    # phrases "знак питання"/"знак оклику" convert.
    src = "позитивний знак Бабінського"
    assert normalize_spoken_punctuation(src, "uk") == "Позитивний знак Бабінського"


def test_dot_in_decimal_is_preserved() -> None:
    assert normalize_spoken_punctuation("three dot five milligrams", "en") == (
        "Three dot five milligrams"
    )


def test_dot_in_domain_is_preserved() -> None:
    assert normalize_spoken_punctuation("email at example dot com", "en") == (
        "Email at example dot com"
    )


def test_dot_as_command_converts() -> None:
    assert normalize_spoken_punctuation("chest pain dot the exam was normal", "en") == (
        "Chest pain. The exam was normal"
    )


def test_period_of_time_is_preserved() -> None:
    # "period of" is the dominant clinical false positive for "period".
    src = "observed over a period of two weeks"
    assert normalize_spoken_punctuation(src, "en") == "Observed over a period of two weeks"


def test_plain_sentence_unchanged_except_capitalization() -> None:
    src = "the patient reports intermittent headaches"
    assert normalize_spoken_punctuation(src, "en") == (
        "The patient reports intermittent headaches"
    )


def test_uk_plain_sentence_unchanged_except_capitalization() -> None:
    src = "пацієнт скаржиться на головний біль"
    assert normalize_spoken_punctuation(src, "uk") == (
        "Пацієнт скаржиться на головний біль"
    )


# ── Edge cases ──────────────────────────────────────────────────────


def test_empty_and_blank_input() -> None:
    assert normalize_spoken_punctuation("", "en") == ""
    assert normalize_spoken_punctuation("   ", "en") == "   "


def test_unknown_language_is_passthrough() -> None:
    assert normalize_spoken_punctuation("pain period", "fr") == "pain period"


def test_longest_phrase_wins() -> None:
    # "крапка з комою" must beat the bare "крапка".
    assert normalize_spoken_punctuation("а крапка з комою б", "uk") == "А; б"


def test_idempotent() -> None:
    once = normalize_spoken_punctuation("pain period nausea comma cough period", "en")
    twice = normalize_spoken_punctuation(once, "en")
    assert once == twice


def test_collapses_redundant_upstream_mark() -> None:
    # Simulates the ML punctuator having already ended the clause before the
    # explicit "period" token ("chest pain." + "Period").
    assert normalize_spoken_punctuation("chest pain. Period oxygen normal", "en") == (
        "Chest pain. Oxygen normal"
    )


# ── Stage wrapper ───────────────────────────────────────────────────


def _ctx(language: str) -> ProcessingContext:
    return ProcessingContext(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        language=language,  # type: ignore[arg-type]
        specialty=None,
        reference_date=date(2026, 1, 1),
        is_partial=False,
        abbreviation_snapshot=AbbreviationSnapshot(entries=(), fingerprint="x"),
        pipeline_version="t",
    )


def test_stage_converts_and_flags_change() -> None:
    stage = SpokenPunctuationStage()
    out = asyncio.run(
        stage.process(_ctx("uk"), StageInput(text="біль крапка задишки немає крапка"))
    )
    assert out.text == "Біль. Задишки немає."
    assert out.metadata["spoken_punctuation.changed"] is True


def test_stage_noop_when_nothing_to_change() -> None:
    stage = SpokenPunctuationStage()
    out = asyncio.run(stage.process(_ctx("en"), StageInput(text="The exam was normal.")))
    assert out.text == "The exam was normal."
    assert out.metadata["spoken_punctuation.changed"] is False


def test_stage_runs_on_partials() -> None:
    assert SpokenPunctuationStage().runs_on_partials is True


# ── Full-pipeline integration (rule-based punctuation fallback) ──────


def _full_pipeline() -> Orchestrator:
    # PunctuationStage is constructed without startup(), so the model is
    # never loaded and it uses its deterministic rule-based fallback —
    # exactly the environment the ASR spec's expected outputs assume.
    return Orchestrator(
        stages=[
            VoiceCommandStage(specs_by_language={}),
            PunctuationStage(),
            SpokenPunctuationStage(),
            NumberNormStage(),
            DateNormStage(),
            AbbreviationStage(),
            ConfidenceStage(),
        ]
    )


def test_full_pipeline_uk_matches_spec() -> None:

    orch = _full_pipeline()
    src = (
        "Пацієнт скаржиться на біль у грудях крапка задишки немає кома "
        "сатурація нормальна крапка"
    )
    out = asyncio.run(orch.run(_ctx("uk"), StageInput(text=src)))
    assert out.text == (
        "Пацієнт скаржиться на біль у грудях. Задишки немає, "
        "сатурація нормальна."
    )


def test_full_pipeline_en_matches_spec() -> None:

    orch = _full_pipeline()
    src = (
        "The patient denies chest pain period oxygen saturation is normal "
        "comma no pleural effusion period"
    )
    out = asyncio.run(orch.run(_ctx("en"), StageInput(text=src)))
    assert out.text == (
        "The patient denies chest pain. Oxygen saturation is normal, "
        "no pleural effusion."
    )
