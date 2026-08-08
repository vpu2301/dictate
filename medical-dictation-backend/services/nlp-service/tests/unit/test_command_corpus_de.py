"""TP/FP gate for the German command catalogue.

The German catalogue ships as ``voice_commands_de.json`` and is loaded
here exactly as the seeder loads it, so the file that reaches the
database is the file under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from nlp_service.pipeline.base import ChoiceOption, TemplateSection, Word
from nlp_service.stages.voice_command_matcher import CommandSpec, VoiceCommandMatcher
from tests.fixtures.command_corpus_de import NEGATIVES, POSITIVES

_SEED_DIR = Path(__file__).resolve().parents[4] / "infra" / "postgres" / "seed"


def _load_specs(language: str = "de") -> list[CommandSpec]:
    raw = json.loads((_SEED_DIR / f"voice_commands_{language}.json").read_text("utf-8"))
    return [
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
        for cmd in raw
    ]


def _sections() -> tuple[TemplateSection, ...]:
    return (
        TemplateSection(
            id=uuid4(),
            name="Raucherstatus",
            aliases=("rauchen",),
            section_key="smoking_status",
            field_type="choice",
            options=(
                ChoiceOption(value="never", label="Nichtraucher", aliases=("nichtraucher",)),
                ChoiceOption(value="current", label="Raucher", aliases=("raucher",)),
            ),
        ),
        TemplateSection(
            id=uuid4(),
            name="Allergien",
            aliases=("allergien",),
            section_key="allergies",
            field_type="multi_choice",
            options=(
                ChoiceOption(value="penicillin", label="Penicillin", aliases=("penicillin",)),
                ChoiceOption(value="latex", label="Latex", aliases=("latex",)),
            ),
        ),
    )


def _words(tokens: list[str], *, lead_pause_ms: int = 500, p: float = 0.95) -> list[Word]:
    """Words with a leading pause, i.e. under the MOST permissive
    conditions a command can fire in. Negatives that stay quiet here stay
    quiet in real speech."""
    out: list[Word] = []
    t = lead_pause_ms / 1000.0
    for token in tokens:
        out.append(Word(text=token, start_s=t, end_s=t + 0.30, probability=p))
        t += 0.32
    return out


def _matcher() -> VoiceCommandMatcher:
    return VoiceCommandMatcher(_load_specs(), language="de", template_sections=_sections())


def _detect(matcher: VoiceCommandMatcher, tokens: list[str]) -> list[str]:
    return [m.slot.intent for m in matcher.detect(_words(tokens))]


def test_catalogue_mirrors_the_english_intent_set() -> None:
    """A missing intent is a German session where a command silently does
    nothing, so the sets must match exactly."""
    de = {c.intent for c in _load_specs("de")}
    en = {c.intent for c in _load_specs("en")}
    assert de == en


@pytest.mark.parametrize(("tokens", "intent", "why"), POSITIVES)
def test_positive_cases(tokens: list[str], intent: str, why: str) -> None:
    assert intent in _detect(_matcher(), tokens), f"{why}: {' '.join(tokens)!r}"


@pytest.mark.parametrize(("tokens", "why"), NEGATIVES)
def test_negative_cases(tokens: list[str], why: str) -> None:
    """Clinical prose must never be consumed as a command."""
    detected = _detect(_matcher(), tokens)
    assert detected == [], f"{why}: {' '.join(tokens)!r} matched {detected}"


def test_recall_and_precision() -> None:
    matcher = _matcher()
    tp = sum(1 for tokens, intent, _ in POSITIVES if intent in _detect(matcher, tokens))
    fp = sum(1 for tokens, _ in NEGATIVES if _detect(matcher, tokens))
    assert fp == 0, "the German catalogue EATS clinical prose — tighten the specs, never the FSM"
    assert tp == len(POSITIVES)
