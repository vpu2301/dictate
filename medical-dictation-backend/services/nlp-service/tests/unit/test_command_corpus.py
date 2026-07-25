"""TP/FP corpus gate — the anamnesis commands must not degrade matching.

Every added spec widens the matcher's surface. The risk is not that a
command fails to fire; it is that clinical prose gets EATEN as a
command, silently deleting words from a note. So the corpus is measured
twice — sprint-05 catalogue alone, then with sprint-13's four specs —
and the two runs are compared.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from nlp_service.pipeline.base import ChoiceOption, TemplateSection, Word
from nlp_service.stages.voice_command_matcher import CommandSpec, VoiceCommandMatcher
from tests.fixtures.command_corpus_uk import NEGATIVES, POSITIVES

_SEED_DIR = Path(__file__).resolve().parents[4] / "infra" / "postgres" / "seed"

_S13_INTENTS = {"choice.set", "choice.add", "choice.remove", "diagnosis.capture"}


def _load_specs(language: str = "uk", *, include_s13: bool) -> list[CommandSpec]:
    """The real shipped catalogue, straight from the seed fixtures."""
    raw = json.loads((_SEED_DIR / f"voice_commands_{language}.json").read_text("utf-8"))
    specs: list[CommandSpec] = []
    for cmd in raw:
        if not include_s13 and cmd["intent"] in _S13_INTENTS:
            continue
        specs.append(
            CommandSpec(
                intent=cmd["intent"],
                language=language,
                phrases=tuple(tuple(p) for p in cmd["phrases"]),
                requires_pause_before_ms=int(cmd.get("requires_pause_before_ms", 200)),
                min_avg_probability=float(cmd.get("min_avg_probability", 0.85)),
                is_section_command=bool(cmd.get("is_section_command", False)),
                is_option_command=bool(cmd.get("is_option_command", False)),
                exact_match_only=bool(cmd.get("exact_match_only", False)),
            )
        )
    return specs


def _sections() -> tuple[TemplateSection, ...]:
    return (
        TemplateSection(
            id=uuid4(),
            name="Статус куріння",
            aliases=("куріння",),
            section_key="smoking_status",
            field_type="choice",
            options=(
                ChoiceOption(value="never", label="не палить", aliases=("не палить", "не курить")),
                ChoiceOption(value="current", label="палить", aliases=("палить", "курить")),
            ),
        ),
        TemplateSection(
            id=uuid4(),
            name="Алергії",
            aliases=("алергії",),
            section_key="allergies",
            field_type="multi_choice",
            options=(
                ChoiceOption(value="penicillin", label="пеніцилін", aliases=("пеніцилін",)),
                ChoiceOption(value="latex", label="латекс", aliases=("латекс",)),
            ),
        ),
    )


def _words(tokens: list[str], *, lead_pause_ms: int = 500, p: float = 0.95) -> list[Word]:
    """Words with a leading pause so command gates can fire.

    The pause is what makes a command a command; prose in mid-sentence
    lacks it. Every corpus utterance is given the pause so the negatives
    are tested under the MOST permissive conditions — if they stay quiet
    here, they stay quiet in real speech.
    """
    out: list[Word] = []
    t = lead_pause_ms / 1000.0
    for token in tokens:
        out.append(Word(text=token, start_s=t, end_s=t + 0.30, probability=p))
        t += 0.32
    return out


def _matcher(*, include_s13: bool) -> VoiceCommandMatcher:
    return VoiceCommandMatcher(
        _load_specs(include_s13=include_s13),
        language="uk",
        template_sections=_sections(),
    )


def _detect(matcher: VoiceCommandMatcher, tokens: list[str]) -> list[str]:
    return [m.slot.intent for m in matcher.detect(_words(tokens))]


# ── the measured gate ───────────────────────────────────────────────


def _measure(include_s13: bool) -> dict[str, float | int]:
    matcher = _matcher(include_s13=include_s13)
    considered = [
        (tokens, intent)
        for tokens, intent, _ in POSITIVES
        if include_s13 or intent not in _S13_INTENTS
    ]
    tp = sum(1 for tokens, intent in considered if intent in _detect(matcher, tokens))
    fp = sum(1 for tokens, _ in NEGATIVES if _detect(matcher, tokens))
    return {
        "positives": len(considered),
        "true_positives": tp,
        "recall": round(tp / len(considered), 4) if considered else 1.0,
        "negatives": len(NEGATIVES),
        "false_positives": fp,
        "fp_rate": round(fp / len(NEGATIVES), 4) if NEGATIVES else 0.0,
    }


def test_corpus_before_and_after_seeding(capsys: pytest.CaptureFixture[str]) -> None:
    """THE GATE: adding the anamnesis specs must not cost recall or
    introduce a single false positive."""
    before = _measure(include_s13=False)
    after = _measure(include_s13=True)

    with capsys.disabled():
        print("\n  sprint-05 catalogue only :", before)
        print("  + sprint-13 anamnesis    :", after)

    assert before["false_positives"] == 0, before
    assert after["false_positives"] == 0, (
        f"the new specs EAT clinical prose: {after} — tighten the new patterns, never the FSM"
    )
    assert before["recall"] == 1.0, before
    assert after["recall"] == 1.0, after
    # Recall on the pre-existing commands must not move.
    assert after["fp_rate"] <= before["fp_rate"]


@pytest.mark.parametrize(("tokens", "intent", "why"), POSITIVES)
def test_positive_cases(tokens: list[str], intent: str, why: str) -> None:
    assert intent in _detect(_matcher(include_s13=True), tokens), f"{why}: {' '.join(tokens)!r}"


@pytest.mark.parametrize(("tokens", "why"), NEGATIVES)
def test_negative_cases(tokens: list[str], why: str) -> None:
    """Clinical prose must never be consumed as a command."""
    detected = _detect(_matcher(include_s13=True), tokens)
    assert detected == [], f"{why}: {' '.join(tokens)!r} matched {detected}"


def test_sprint_05_intents_behave_identically_before_and_after() -> None:
    """Per-utterance equality, not just aggregate parity."""
    before, after = _matcher(include_s13=False), _matcher(include_s13=True)
    for tokens, intent, why in POSITIVES:
        if intent in _S13_INTENTS:
            continue
        assert _detect(before, tokens) == _detect(after, tokens), why
