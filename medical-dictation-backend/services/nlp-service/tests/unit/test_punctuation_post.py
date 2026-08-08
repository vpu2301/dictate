"""Punctuation post-edit tests (no model required)."""

from __future__ import annotations

from nlp_service.stages.punctuation_post import (
    capitalize_first_letter,
    capitalize_post_punctuation,
    lowercase_units_after_numbers,
    strip_double_punctuation,
)


def test_capitalize_first_letter_basic() -> None:
    assert capitalize_first_letter("hello") == "Hello"


def test_capitalize_first_letter_preserves_leading_ws() -> None:
    assert capitalize_first_letter("   hello") == "   Hello"


def test_capitalize_after_period() -> None:
    assert capitalize_post_punctuation("hello. world") == "hello. World"


def test_capitalize_after_question_mark() -> None:
    assert capitalize_post_punctuation("ok? yes") == "ok? Yes"


def test_lowercase_units_uk() -> None:
    assert lowercase_units_after_numbers("120 МГ", "uk") == "120 мг"
    assert lowercase_units_after_numbers("5 МЛ", "uk") == "5 мл"


def test_lowercase_units_en() -> None:
    assert lowercase_units_after_numbers("5 MG", "en") == "5 mg"


def test_lowercase_units_preserves_non_units() -> None:
    assert lowercase_units_after_numbers("5 ДНІВ", "uk") == "5 ДНІВ"


def test_strip_double_punctuation() -> None:
    assert strip_double_punctuation("hello..") == "hello."
    assert strip_double_punctuation("yes!!") == "yes!"
    assert strip_double_punctuation("ok, , done") == "ok, done"


def test_lowercase_units_de() -> None:
    assert lowercase_units_after_numbers("5 MG", "de") == "5 mg"
    assert lowercase_units_after_numbers("20 ML", "de") == "20 ml"


def test_lowercase_units_de_preserves_nouns_and_ie() -> None:
    """German capitalizes nouns, and "IE" (internationale Einheiten) is
    written upper-case — neither may be lower-cased just for following a
    number."""
    assert lowercase_units_after_numbers("5 Tabletten", "de") == "5 Tabletten"
    assert lowercase_units_after_numbers("1000 IE", "de") == "1000 IE"


def test_capitalize_after_period_umlaut() -> None:
    assert capitalize_post_punctuation("Befund. übelkeit besteht") == "Befund. Übelkeit besteht"
