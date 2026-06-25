"""Ukrainian number normalization corpus.

Each row is a hand-authored (input, expected) pair representing the
patterns clinicians actually dictate. Sprint-5 target: ≥ 95% pass on
the full set per language.
"""

from __future__ import annotations

import pytest

from nlp_service.stages.number_norm_uk import normalize_uk

CASES_UK: list[tuple[str, str]] = [
    # ── BP patterns ─────────────────────────────────────────────
    ("тиск сто двадцять на вісімдесят", "тиск 120/80"),
    ("тиск сто двадцять на вісімдесят міліметрів ртутного стовпчика", "тиск 120/80 мм рт. ст."),
    ("тиск 130 на 90", "тиск 130/90"),
    # ── HR ──────────────────────────────────────────────────────
    ("пульс сімдесят два за хвилину", "пульс 72/хв"),
    ("пульс 80 ударів за хвилину", "пульс 80/хв"),
    # ── Dose / units ────────────────────────────────────────────
    ("прийняти п'ять міліграм", "прийняти 5 мг"),
    ("сто міліграмів", "100 мг"),
    ("двадцять мілілітрів", "20 мл"),
    ("сімдесят кілограмів", "70 кг"),
    # ── Decimal ─────────────────────────────────────────────────
    ("сім цілих п'ять", "7,5"),
    # ── Range ───────────────────────────────────────────────────
    ("від сто до сто двадцять", "100–120"),
    ("температура від тридцяти шести до тридцяти восьми", "температура 36–38"),
    # ── Time ────────────────────────────────────────────────────
    ("о пів на восьму", "07:30"),
    # ── Pass-through (no markers) ──────────────────────────────
    ("один пацієнт", "один пацієнт"),
    # ── Frequency ───────────────────────────────────────────────
    ("три рази на добу", "3 разів/добу"),
    # ── Clinical-safety: no dropped / wrong digits (ADR-0015) ────
    # Decimal fraction with a leading zero must survive: 2.05, not 2.0.
    ("два цілих нуль п'ять", "2,05"),
    # "на" is a common preposition — only a plausible BP (or a BP cue)
    # may become a slash, so "три на чотири" must pass through.
    ("три на чотири", "три на чотири"),
    # A plausible BP without an explicit cue word still normalizes.
    ("сто двадцять на вісімдесят", "120/80"),
]


@pytest.mark.parametrize("raw,expected", CASES_UK)
def test_uk(raw: str, expected: str) -> None:
    assert normalize_uk(raw, decimal_separator=",", bp_separator="/") == expected
