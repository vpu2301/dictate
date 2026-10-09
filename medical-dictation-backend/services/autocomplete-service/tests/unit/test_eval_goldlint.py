"""The gold-transcript linter (corpus-v3 Epic B).

Two properties carry the whole feature.

The first is Epic B's stated acceptance criterion: a gold reading "пульс
68/хв" must produce a warning AND the rewrite "пульс шістдесят вісім за
хвилину". A linter that only complains moves the work onto the author and
gets switched off.

The second is the safety one, and it is why the suggestion can be trusted
at all: no proposal may change the measurement. Grammar the rewriter gets
approximately right; the number it must get exactly right, because a
suggestion that turned 5 mg into 50 mg and was accepted with one click
would be a clinical-safety bug wearing a convenience feature's clothes.
"""

from __future__ import annotations

import pytest
from autocomplete_service import eval_goldlint as lint
from autocomplete_service import eval_normalize as norm
from autocomplete_service.eval_script import SCRIPT, gold_transcript
from hypothesis import given, settings
from hypothesis import strategies as st


def test_epic_b_acceptance_criterion():
    """The example from the spec, asserted verbatim."""
    result = lint.lint("пульс 68/хв", "uk")
    assert not result.clean
    assert set(result.codes) == {"digits_in_gold", "slash_in_gold"}
    assert result.suggestion == "пульс шістдесят вісім за хвилину"


@pytest.mark.parametrize(
    ("text", "codes"),
    [
        ("Артеріальний тиск 140/90 мм рт. ст.", {"digits_in_gold", "slash_in_gold"}),
        ("Глікований гемоглобін 7,2 %.", {"digits_in_gold", "percent_in_gold"}),
        ("Читайте  спокійно", {"double_space"}),
        ("Читайте спокійно ", {"edge_whitespace"}),
    ],
)
def test_findings(text, codes):
    assert set(lint.lint(text, "uk").codes) == codes


def test_compliant_gold_is_silent():
    """The style guide's own examples must not trip their own linter."""
    for row in SCRIPT:
        result = lint.lint_gold(
            say=row["say"], transcript=gold_transcript(row), language=row["language"]
        )
        assert result.clean, row["id"]


def test_identifiers_are_flagged_but_partially_fixable():
    """"HbA1c" is a name, not a measurement.

    The linter still warns — it cannot know — but the rewriter leaves the
    assay name alone and converts the values around it, so the author gets a
    useful proposal instead of a dead end.
    """
    result = lint.lint("HbA1c 6,9, глюкоза натще 5,8.", "uk")
    assert result.codes == ("digits_in_gold",)
    assert result.suggestion is not None
    assert result.suggestion.startswith("HbA1c шість цілих дев'ять десятих")


def test_a_slash_between_words_gets_no_invented_fix():
    """"амоксицилін/клавуланова кислота" is not a measurement; the numbers
    beside it are. The proposal fixes what it understands and leaves the
    rest flagged for a human rather than guessing."""
    result = lint.lint(
        "амоксицилін/клавуланова кислота 875/125.", "uk"
    )
    assert "slash_in_gold" in result.codes
    assert result.suggestion is not None
    assert "амоксицилін/клавуланова" in result.suggestion
    assert "вісімсот сімдесят п'ять на сто двадцять п'ять" in result.suggestion


def test_gold_equal_to_say_is_never_linted():
    """The style guide's answer is "write the gold as it is spoken"; a line
    that stores no separate gold has already complied."""
    assert lint.lint_gold(
        say="пульс 68/хв", transcript=None, language="uk"
    ).clean
    assert lint.lint_gold(
        say="пульс 68/хв", transcript="пульс 68/хв", language="uk"
    ).clean


@settings(max_examples=300, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            ["140/90", "68/хв", "7,2%", "5", "мг", "1000", "мг", "12×8", "мм", "875/125", "0,5", "мл", "HbA1c", "B12", "тиск", "пульс", "температура", "призначено", "двічі", "на", "добу", "протягом", "5", "mg", "138/84", "66", "bpm", "2.5", "mg", "98", "%", "ten", "days"]
        ),
        min_size=1,
        max_size=8,
    ),
    st.sampled_from(("uk", "en")),
)
def test_a_suggestion_never_changes_the_measurement(fragments, language):
    """The safety property. Whatever the rewriter proposes, the canonical
    reading and the ordered list of numbers-and-units must be identical to
    the text it replaces — or no proposal is made at all."""
    text = " ".join(fragments)
    suggestion = lint.lint(text, language).suggestion
    if suggestion is None:
        return
    assert norm.normalize(suggestion, language) == norm.normalize(text, language)
    assert norm.numeric_signature(suggestion, language) == norm.numeric_signature(
        text, language
    )


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            ["140/90", "68/хв", "7,2%", "5", "мг", "875/125", "0,5", "мл", "тиск", "пульс", "на", "добу"]
        ),
        min_size=1,
        max_size=6,
    )
)
def test_a_suggestion_is_never_worse_than_what_it_replaces(fragments):
    """Applying the fix must move the line toward the style guide, never
    away from it — otherwise the sweep could oscillate."""
    text = " ".join(fragments)
    suggestion = lint.lint(text, "uk").suggestion
    if suggestion is None:
        return
    assert lint._severity(suggestion) <= lint._severity(text)
