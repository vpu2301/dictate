"""Suggest dispatch tests — covers snippet prefix routing + extraction."""

from __future__ import annotations

from autocomplete_service.suggest import (
    extract_snippet_trigger,
    is_snippet_prefix,
)


def test_slash_prefix_routes_to_snippet():
    assert is_snippet_prefix("/cv")
    assert is_snippet_prefix("/vitals")


def test_non_slash_prefix_routes_to_trie():
    assert not is_snippet_prefix("задишк")
    assert not is_snippet_prefix("chest")


def test_trigger_extraction_lowercases_strips_slashes():
    assert extract_snippet_trigger("/CV") == "cv"
    assert extract_snippet_trigger("//VITALS") == "vitals"
    assert extract_snippet_trigger("/  trim  ") == "trim"


def test_trigger_extraction_caps_at_32_chars():
    long_trigger = "/" + ("a" * 50)
    assert len(extract_snippet_trigger(long_trigger)) == 32
