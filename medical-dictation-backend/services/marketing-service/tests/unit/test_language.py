"""The language choice — the rule the emails have to get right."""

from __future__ import annotations

import pytest

from marketing_service.domain.language import (
    SUPPORTED,
    lang_from_country,
    normalise_country,
    normalise_lang,
    parse_accept_language,
    resolve_language,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("de", "de"),
        ("de-AT", "de"),
        ("DE_de", "de"),
        ("  Uk  ", "uk"),
        ("en-GB", "en"),
        ("fr", None),
        ("", None),
        (None, None),
    ],
)
def test_normalise_lang(raw: str | None, expected: str | None) -> None:
    assert normalise_lang(raw) == expected


def test_normalise_country_rejects_non_alpha2() -> None:
    assert normalise_country("ua") == "UA"
    assert normalise_country("UKR") is None
    assert normalise_country("U1") is None
    assert normalise_country(None) is None


def test_country_maps_the_german_speaking_market() -> None:
    assert lang_from_country("DE") == "de"
    assert lang_from_country("AT") == "de"
    assert lang_from_country("CH") == "de"
    assert lang_from_country("UA") == "uk"
    # Not mapped: falls through to the next signal, not to German.
    assert lang_from_country("PL") is None


def test_accept_language_orders_by_quality() -> None:
    assert parse_accept_language("uk-UA,uk;q=0.9,en;q=0.8") == ["uk-UA", "uk", "en"]
    assert parse_accept_language("en;q=0.2,de;q=0.9") == ["de", "en"]


def test_accept_language_survives_a_malformed_q() -> None:
    # Attacker-controlled input. Must sort last, never raise.
    assert parse_accept_language("de;q=,uk") == ["uk", "de"]


def test_form_language_beats_country() -> None:
    """A Ukrainian reading the German page gets German.

    The page they chose is a decision; the country they are sitting in
    is an inference.
    """
    assert resolve_language(form_lang="de", country="UA") == "de"


def test_country_decides_when_the_form_says_nothing() -> None:
    assert resolve_language(form_lang=None, country="AT") == "de"
    assert resolve_language(form_lang=None, country="UA") == "uk"


def test_accept_language_is_the_last_resort() -> None:
    assert resolve_language(country="PL", accept_language="de-DE,de;q=0.9") == "de"


def test_unsupported_form_language_falls_through_rather_than_stopping() -> None:
    """`fr` must not pin the answer to English before the country is read."""
    assert resolve_language(form_lang="fr", country="UA") == "uk"


def test_everything_unknown_is_english() -> None:
    assert resolve_language() == "en"
    assert resolve_language(form_lang="fr", country="PL", accept_language="fr,es") == "en"


def test_resolve_always_returns_a_supported_language() -> None:
    for candidate in ("", "xx", "de", "uk", None):
        assert resolve_language(form_lang=candidate) in SUPPORTED
