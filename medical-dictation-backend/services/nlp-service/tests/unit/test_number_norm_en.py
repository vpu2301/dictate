"""English number normalization corpus."""

from __future__ import annotations

import pytest

from nlp_service.stages.number_norm_en import normalize_en

CASES_EN: list[tuple[str, str]] = [
    # ── BP ─────────────────────────────────────────────────────
    ("blood pressure one twenty over eighty",
     "blood pressure 120/80"),
    ("blood pressure one twenty over eighty millimeters of mercury",
     "blood pressure 120/80 mmHg"),
    ("BP 130 over 90", "BP 130/90"),
    # ── Decimal ────────────────────────────────────────────────
    ("seven point five", "7.5"),
    # ── Dose / units ───────────────────────────────────────────
    ("five milligrams", "5 mg"),
    ("one hundred milligrams", "100 mg"),
    ("twenty milliliters", "20 ml"),
    # ── Range ──────────────────────────────────────────────────
    ("from one hundred to one twenty", "100–120"),
    # ── Time ───────────────────────────────────────────────────
    ("half past seven", "07:30"),
    # ── Frequency ──────────────────────────────────────────────
    ("three times a day", "3x/day"),
    # ── Pass-through ──────────────────────────────────────────
    ("one patient", "one patient"),
]


@pytest.mark.parametrize("raw,expected", CASES_EN)
def test_en(raw: str, expected: str) -> None:
    assert normalize_en(raw, decimal_separator=".", bp_separator="/") == expected
