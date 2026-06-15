"""Rule-based post-edits applied AFTER punctuation (model or fallback).

These are deterministic and always run. They patch the cases the
transformer model gets wrong frequently enough that piloting will
notice — most importantly unit casing after numbers (mg/ml/mmHg).
"""

from __future__ import annotations

import re

# Known medical units; lowercase canonical form.
_UNITS_UK = {
    "мг",
    "мл",
    "см",
    "м",
    "мм",
    "кг",
    "г",
    "л",
    "мкг",
    "мкл",
    "ммоль",
    "од",
    "хв",
    "сек",
}
_UNITS_EN = {
    "mg",
    "ml",
    "cm",
    "mm",
    "kg",
    "g",
    "l",
    "ug",
    "mcg",
    "mmol",
    "iu",
    "bpm",
}
_COMPOUND_UK = ["мм рт. ст.", "кг/м²", "м²", "г/л", "мг/кг"]
_COMPOUND_EN = ["mm hg", "mmhg", "kg/m²", "m²", "g/l", "mg/kg"]


def capitalize_first_letter(text: str) -> str:
    """Capitalize the first alphabetic character."""
    s = text.lstrip()
    if not s:
        return text
    return text[: len(text) - len(s)] + s[0].upper() + s[1:]


_SENTENCE_END = re.compile(r"([.!?])\s+([a-zа-яёіїєґ])", re.IGNORECASE | re.UNICODE)


def capitalize_post_punctuation(text: str) -> str:
    """After . ! ? + whitespace, force the next letter to uppercase."""

    def _up(match: re.Match[str]) -> str:
        return match.group(1) + " " + match.group(2).upper()

    return _SENTENCE_END.sub(_up, text)


_NUMBER_FOLLOWED_BY_WORD = re.compile(
    r"(\d+(?:[.,]\d+)?)\s+([A-Za-zА-Яа-яЁёІіЇїЄєҐґ]+)",
    re.UNICODE,
)


def lowercase_units_after_numbers(text: str, language: str) -> str:
    """If a known unit follows a number, force the unit to its canonical
    lowercase form. ``"120 МГ"`` → ``"120 мг"``."""
    units = _UNITS_UK if language == "uk" else _UNITS_EN

    def _conv(match: re.Match[str]) -> str:
        num, word = match.group(1), match.group(2)
        lc = word.lower()
        if lc in units:
            return f"{num} {lc}"
        return match.group(0)

    return _NUMBER_FOLLOWED_BY_WORD.sub(_conv, text)


_DOUBLES = re.compile(r"([.!?,])\s*\1+")


def strip_double_punctuation(text: str) -> str:
    """Collapse `..` → `.`, `,,` → `,`, etc.

    The transformer model occasionally double-punctuates at chunk
    boundaries; the fallback can add a period at end of a sentence the
    model already ended.
    """
    return _DOUBLES.sub(r"\1", text)
