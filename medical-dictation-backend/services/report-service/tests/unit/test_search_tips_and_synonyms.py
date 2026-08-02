"""Search tips content + synonym CRUD wire models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from report_service.routers.search_tips import _TIPS
from report_service.routers.synonyms import SynonymGroupBody


def test_tips_exist_in_both_languages_with_same_keys():
    assert set(_TIPS) == {"uk", "en"}
    uk_keys = [t.key for t in _TIPS["uk"]]
    en_keys = [t.key for t in _TIPS["en"]]
    assert uk_keys == en_keys
    assert {"no_stemming", "synonyms", "filters_and"} <= set(uk_keys)
    # The honesty contract: tips must state the no-stemming limitation and
    # the expand=false escape hatch.
    uk_all = " ".join(t.body for t in _TIPS["uk"])
    assert "не відмінює" in uk_all
    assert "expand=false" in uk_all


def test_synonym_body_valid():
    body = SynonymGroupBody(terms=["ХСН ", "хронічна серцева недостатність"], language="uk")
    assert body.terms[0] == "ХСН"  # trimmed


def test_synonym_body_rejects_single_term():
    with pytest.raises(ValidationError):
        SynonymGroupBody(terms=["ХСН"], language="uk")


def test_synonym_body_rejects_duplicates_case_insensitive():
    with pytest.raises(ValidationError, match="duplicate term"):
        SynonymGroupBody(terms=["ХСН", "хсн"], language="uk")


def test_synonym_body_rejects_oversized_term():
    with pytest.raises(ValidationError):
        SynonymGroupBody(terms=["a" * 121, "b"], language="en")


def test_synonym_body_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        SynonymGroupBody(terms=["a", "b"], language="uk", source="system")
