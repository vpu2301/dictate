"""Generator output handling — strict JSON, dropped not repaired (ADR-0044 §4).

The generation *call* lives in adapters/llm.py; this module owns the box:
schema validation, tier floor, and the honest accounting of what was dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, ValidationError

from corpus_forge.domain.ngram import within_gates


class GeneratedPhrase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrase: str
    language: str
    specialty: str
    section: str


@dataclass(frozen=True, slots=True)
class GenerationParse:
    kept: list[GeneratedPhrase]
    dropped_malformed: int
    dropped_gates: int


def parse_generation_batch(
    raw: str,
    *,
    language: str,
    specialty: str,
    section: str,
) -> GenerationParse:
    """Parse a strict JSON array of phrase objects. Malformed elements are
    dropped, never repaired; rows contradicting the requested cell are
    malformed by definition."""
    try:
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end <= start:
            return GenerationParse(kept=[], dropped_malformed=1, dropped_gates=0)
        items = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return GenerationParse(kept=[], dropped_malformed=1, dropped_gates=0)
    if not isinstance(items, list):
        return GenerationParse(kept=[], dropped_malformed=1, dropped_gates=0)

    kept: list[GeneratedPhrase] = []
    malformed = 0
    gated = 0
    for item in items:
        try:
            row = GeneratedPhrase.model_validate(item)
        except ValidationError:
            malformed += 1
            continue
        if (row.language, row.specialty, row.section) != (language, specialty, section):
            malformed += 1
            continue
        if not within_gates(row.phrase):
            gated += 1
            continue
        kept.append(row)
    return GenerationParse(kept=kept, dropped_malformed=malformed, dropped_gates=gated)


def build_generation_prompt(
    *,
    language: str,
    specialty: str,
    section: str,
    seed_terms: list[str],
    avoid_phrases: list[str],
    target_count: int,
) -> str:
    lang_name = {"uk": "Ukrainian", "en": "English"}.get(language, language)
    avoid_block = "\n".join(f"- {p}" for p in avoid_phrases[:100])
    seed_block = "\n".join(f"- {t}" for t in seed_terms[:50])
    return (
        f"You write short {lang_name} clinical dictation phrases for the "
        f"'{section}' section of a {specialty} report.\n"
        f"Produce exactly {target_count} distinct phrases a clinician would "
        "realistically dictate. 3-12 words each, at most 80 characters, no "
        "patient identifiers, no numbers or doses unless the seed term "
        "contains one.\n"
        + (f"Ground them in these terms:\n{seed_block}\n" if seed_block else "")
        + (f"Do NOT duplicate or closely paraphrase:\n{avoid_block}\n" if avoid_block else "")
        + "Answer with ONLY a JSON array, no prose, where every element is "
        '{"phrase": "...", "language": "' + language + '", "specialty": "'
        + specialty + '", "section": "' + section + '"}'
    )
