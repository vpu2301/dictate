"""prepare(): normalization, uk/en lexicon expansion (lexical path only), lang guess."""

from __future__ import annotations

from evidence_retrieval.domain.preprocess import LEXICON_VERSION, prepare


def test_ukrainian_abbreviation_expands_for_lexical_path_only() -> None:
    prepared = prepare("лікування АГ у пацієнтів")

    assert "артеріальна гіпертензія" in prepared.expanded_terms
    assert "артеріальна гіпертензія" in prepared.lexical_query
    # normalized stays the raw (whitespace-normalized) text — dense path untouched.
    assert prepared.normalized == "лікування АГ у пацієнтів"
    assert prepared.raw == "лікування АГ у пацієнтів"


def test_english_abbreviation_expands() -> None:
    prepared = prepare("CKD dosing")

    assert prepared.expanded_terms == ["chronic kidney disease"]
    assert prepared.lexical_query == "CKD dosing chronic kidney disease"


def test_unknown_tokens_produce_no_expansions() -> None:
    prepared = prepare("amoxicillin dosing in children")

    assert prepared.expanded_terms == []


def test_duplicate_abbreviations_expand_once() -> None:
    prepared = prepare("АГ та знову АГ")

    assert prepared.expanded_terms == ["артеріальна гіпертензія"]

    # Distinct abbreviations mapping to the same expansion also dedupe (ішс/іхс).
    prepared = prepare("ішс або іхс")
    assert prepared.expanded_terms == ["ішемічна хвороба серця"]


def test_lang_guess_cyrillic_latin_and_neither() -> None:
    assert prepare("лікування гіпертензії").lang_guess == "uk"
    assert prepare("hypertension treatment").lang_guess == "en"
    assert prepare("123").lang_guess is None


def test_whitespace_normalization() -> None:
    prepared = prepare("  CKD \t dosing \n guidance  ")

    assert prepared.normalized == "CKD dosing guidance"
    assert prepared.raw == "  CKD \t dosing \n guidance  "


def test_lexical_query_equals_normalized_without_expansions() -> None:
    prepared = prepare("aspirin  after   stroke")

    assert prepared.expanded_terms == []
    assert prepared.lexical_query == prepared.normalized == "aspirin after stroke"


def test_lexicon_version_carried() -> None:
    assert prepare("anything").lexicon_version == LEXICON_VERSION
