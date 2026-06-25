"""Voice command matcher coverage.

Each test corresponds to a spec §3.4 gate (true positive, false positive
on confusable utterances, pause-before, confidence, section args,
mixed-content, edit-distance).
"""

from __future__ import annotations

from uuid import uuid4

from nlp_service.pipeline.base import TemplateSection, Word
from nlp_service.stages.voice_command_matcher import (
    CommandSpec,
    VoiceCommandMatcher,
)


def _w(text: str, start: float, end: float, p: float = 0.95) -> Word:
    return Word(text=text, start_s=start, end_s=end, probability=p)


def _newparagraph_spec_uk() -> CommandSpec:
    return CommandSpec(
        intent="newparagraph",
        language="uk",
        phrases=(("новий", "абзац"),),
        requires_pause_before_ms=200,
        min_avg_probability=0.85,
    )


def _period_spec_uk() -> CommandSpec:
    return CommandSpec(
        intent="period",
        language="uk",
        phrases=(("крапка",),),
        requires_pause_before_ms=300,
        min_avg_probability=0.88,
    )


def test_canonical_match_fires() -> None:
    m = VoiceCommandMatcher([_newparagraph_spec_uk()], language="uk")
    words = [
        _w("тиск", 0.0, 0.3),
        _w("сто", 0.4, 0.6),
        _w("новий", 1.5, 1.7),  # pause = 1500-600 = 900 ms
        _w("абзац", 1.8, 2.0),
    ]
    results = m.detect(words)
    assert len(results) == 1
    assert results[0].slot.intent == "newparagraph"
    assert results[0].ambiguous_with == ()  # only one command matches


def test_ambiguous_match_is_flagged() -> None:
    # Two distinct single-word intents whose phrases are within edit
    # distance of the spoken token both clear the gates → ambiguous.
    spec_a = CommandSpec(
        intent="delete_that",
        language="uk",
        phrases=(("видалити",),),
        requires_pause_before_ms=0,
        min_avg_probability=0.5,
    )
    spec_b = CommandSpec(
        intent="undo_that",
        language="uk",
        phrases=(("видалити",),),  # same surface form, different intent
        requires_pause_before_ms=0,
        min_avg_probability=0.5,
    )
    m = VoiceCommandMatcher([spec_a, spec_b], language="uk")
    results = m.detect([_w("видалити", 0.0, 0.4)])
    assert len(results) == 1
    assert results[0].ambiguous_with  # non-empty: the other intent collided
    assert "undo_that" in results[0].ambiguous_with or "delete_that" in results[0].ambiguous_with


def test_pause_before_not_satisfied_rejects() -> None:
    m = VoiceCommandMatcher([_newparagraph_spec_uk()], language="uk")
    words = [
        _w("тиск", 0.0, 0.3),
        _w("новий", 0.31, 0.5),  # 10 ms gap — well below 200 ms
        _w("абзац", 0.6, 0.8),
    ]
    assert m.detect(words) == []


def test_confidence_below_threshold_rejects() -> None:
    m = VoiceCommandMatcher([_newparagraph_spec_uk()], language="uk")
    words = [
        _w("тиск", 0.0, 0.3),
        _w("новий", 1.5, 1.7, p=0.5),
        _w("абзац", 1.8, 2.0, p=0.6),
    ]
    assert m.detect(words) == []


def test_edit_distance_one_substitution_accepted() -> None:
    m = VoiceCommandMatcher([_newparagraph_spec_uk()], language="uk")
    words = [
        _w("тиск", 0.0, 0.3),
        _w("новий", 1.5, 1.7),
        _w("абсац", 1.8, 2.0),  # 1-char substitution
    ]
    results = m.detect(words)
    assert len(results) == 1


def test_edit_distance_two_substitutions_rejected() -> None:
    m = VoiceCommandMatcher([_newparagraph_spec_uk()], language="uk")
    words = [
        _w("тиск", 0.0, 0.3),
        _w("новив", 1.5, 1.7),  # 1 sub
        _w("абсас", 1.8, 2.0),  # 2 subs → 2 phrases altered → reject
    ]
    assert m.detect(words) == []


def test_mid_phrase_period_not_fired_without_pause() -> None:
    """The famous "крапка над і" idiom — never an actual period command."""
    m = VoiceCommandMatcher([_period_spec_uk()], language="uk")
    words = [
        _w("пацієнт", 0.0, 0.4),
        _w("пройшов", 0.45, 0.7),
        _w("крапка", 0.71, 0.9),  # 10 ms gap
        _w("над", 0.95, 1.1),
        _w("і", 1.15, 1.2),
    ]
    assert m.detect(words) == []


def test_section_command_resolves_against_template() -> None:
    section_id = uuid4()
    spec = CommandSpec(
        intent="section",
        language="uk",
        phrases=(("розділ",),),
        requires_pause_before_ms=200,
        min_avg_probability=0.85,
        is_section_command=True,
    )
    m = VoiceCommandMatcher(
        [spec],
        language="uk",
        template_sections=(TemplateSection(id=section_id, name="діагноз", aliases=("dx",)),),
    )
    words = [
        _w("note", 0.0, 0.3),
        _w("розділ", 1.5, 1.8),
        _w("діагноз", 1.9, 2.2),
    ]
    results = m.detect(words)
    assert len(results) == 1
    assert results[0].slot.intent == f"section.{section_id}"
    assert results[0].slot.arg == {"section_id": str(section_id)}


def test_section_command_without_known_section_rejected() -> None:
    spec = CommandSpec(
        intent="section",
        language="uk",
        phrases=(("розділ",),),
        requires_pause_before_ms=200,
        min_avg_probability=0.85,
        is_section_command=True,
    )
    m = VoiceCommandMatcher([spec], language="uk", template_sections=())
    words = [_w("foo", 0.0, 0.2), _w("розділ", 1.5, 1.8), _w("план", 1.9, 2.1)]
    assert m.detect(words) == []


def test_longest_match_wins() -> None:
    short = CommandSpec(
        intent="paragraph",
        language="uk",
        phrases=(("абзац",),),
        requires_pause_before_ms=200,
        min_avg_probability=0.85,
    )
    long_ = CommandSpec(
        intent="newparagraph",
        language="uk",
        phrases=(("новий", "абзац"),),
        requires_pause_before_ms=200,
        min_avg_probability=0.85,
    )
    m = VoiceCommandMatcher([short, long_], language="uk")
    words = [
        _w("foo", 0.0, 0.3),
        _w("новий", 1.5, 1.7),
        _w("абзац", 1.8, 2.0),
    ]
    results = m.detect(words)
    assert len(results) == 1
    assert results[0].slot.intent == "newparagraph"
