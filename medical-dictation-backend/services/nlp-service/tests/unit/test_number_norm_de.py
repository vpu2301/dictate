"""German number normalization corpus.

Mirrors the UK/EN corpora, plus the two German-specific hazards: the
compound numeral ("zweiundzwanzig" is one token, units-before-tens) and
"zu" as a BP separator in a language where "zu" is also the most common
preposition there is.
"""

from __future__ import annotations

import pytest

from nlp_service.stages.number_norm_de import normalize_de

CASES_DE: list[tuple[str, str]] = [
    # ── BP ─────────────────────────────────────────────────────
    ("Blutdruck hundertvierzig zu neunzig", "Blutdruck 140/90"),
    ("Blutdruck 140 zu 90 Millimeter Quecksilbersäule", "Blutdruck 140/90 mmHg"),
    ("RR 130 zu 85", "RR 130/85"),
    ("120 auf 80 mmHg", "120/80 mmHg"),
    # ── Decimal ────────────────────────────────────────────────
    ("sieben Komma fünf", "7,5"),
    ("siebenunddreißig Komma zwei Grad Celsius", "37,2 °C"),
    # ── Dose / units ───────────────────────────────────────────
    ("fünf Milligramm", "5 mg"),
    ("zweihundert Milligramm", "200 mg"),
    ("zwanzig Milliliter", "20 ml"),
    ("einhundertfünfundvierzig Kilogramm", "145 kg"),
    ("zweitausendfünfhundert Milliliter", "2500 ml"),
    # ── Range ──────────────────────────────────────────────────
    ("von zehn bis zwanzig Milliliter", "10–20 ml"),
    # ── Frequency / rate ───────────────────────────────────────
    ("dreimal täglich", "3x/Tag"),
    ("500 mg zweimal täglich", "500 mg 2x/Tag"),
    ("drei mal pro Tag", "3x/Tag"),
    ("Puls achtzig pro Minute", "Puls 80/min"),
    # ── Pass-through ───────────────────────────────────────────
    ("eine Tablette", "eine Tablette"),
    ("acht Stunden", "acht Stunden"),
    # ── Clinical safety (ADR-0015): no fabricated numbers ──────
    # "zu" outside a BP context must not become a slash.
    ("drei zu vier", "drei zu vier"),
    ("der Patient kam um acht", "der Patient kam um acht"),
    # Decimal fractions keep leading zeros — 5,05 must not collapse to 5,5.
    ("fünf Komma null fünf Milligramm", "5,05 mg"),
    # Ordinary words that merely CONTAIN a numeral substring stay words.
    ("Befund unauffällig und gesund", "Befund unauffällig und gesund"),
    ("Grundumsatz erhöht", "Grundumsatz erhöht"),
    ("manchmal Schwindel", "manchmal Schwindel"),
]


@pytest.mark.parametrize(("raw", "expected"), CASES_DE)
def test_de(raw: str, expected: str) -> None:
    assert normalize_de(raw, decimal_separator=",", bp_separator="/") == expected


COMPOUNDS: list[tuple[str, str]] = [
    ("zweiundzwanzig Milligramm", "22 mg"),
    ("sechsundsechzig Kilogramm", "66 kg"),
    ("neunundneunzig Milliliter", "99 ml"),
    ("hundert Milligramm", "100 mg"),
    ("einhundert Milligramm", "100 mg"),
    ("dreihundertfünfzig Milliliter", "350 ml"),
    ("tausend Milligramm", "1000 mg"),
    ("zwölf Milligramm", "12 mg"),
    ("dreißig Milligramm", "30 mg"),
    ("dreissig Milligramm", "30 mg"),
]


@pytest.mark.parametrize(("raw", "expected"), COMPOUNDS)
def test_compound_numerals(raw: str, expected: str) -> None:
    """Units-before-tens is the German ordering: "vierundzwanzig" is 24,
    never 420."""
    assert normalize_de(raw, decimal_separator=",", bp_separator="/") == expected


def test_bp_without_cue_but_plausible_values() -> None:
    assert normalize_de("140 zu 90", decimal_separator=",", bp_separator="/") == "140/90"


def test_bp_implausible_values_pass_through() -> None:
    """Two numbers joined by "zu" are only a blood pressure when they
    could be one — otherwise the words survive untouched."""
    assert normalize_de("zwei zu drei", decimal_separator=",", bp_separator="/") == "zwei zu drei"


def test_separators_are_honoured() -> None:
    out = normalize_de("Blutdruck 140 zu 90", decimal_separator=".", bp_separator="-")
    assert out == "Blutdruck 140-90"
    assert normalize_de("sieben Komma fünf", decimal_separator=".", bp_separator="/") == "7.5"
