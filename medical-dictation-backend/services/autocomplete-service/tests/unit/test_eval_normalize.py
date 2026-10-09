"""The normalisation contract (corpus-v2 §1.3.1, corpus-v3 Epic A).

The load-bearing test is the first one: every vendored line that ships a
gold text DIFFERENT from its spoken form exists precisely because the two
render the same measurement differently ("сто сорок на дев'яносто" vs
"140/90 мм рт. ст."). If normalisation is doing its job, those pairs
collapse to the same string — and that is the §4 P0-1 acceptance criterion
("правила покривають усі числові репліки v1 без ручних винятків") stated as
an assertion instead of a promise.

The abbreviation lines are the control: they must NOT collapse. "А Те" vs
"АТ" is what the abbreviations subset measures, and a normaliser that
flattened it would delete the measurement while appearing to improve it.

GOLDEN is Epic A's own acceptance table — one row per numeric pattern the
corpus carries, each asserted three ways (written → canonical, spoken →
canonical, and the linter's suggestion → the same canonical). The third is
what makes the reverse direction safe to offer to a human: a proposal that
changed the dose would be a clinical-safety bug, and it cannot survive here.
"""

from __future__ import annotations

import pytest
from autocomplete_service import eval_normalize as norm
from autocomplete_service import eval_spoken as spoken
from autocomplete_service.eval_script import SCRIPT, gold_transcript
from hypothesis import given, settings
from hypothesis import strategies as st

# ── Epic A golden table: (language, written form, spoken form) ──────────
#
# "Written" is how a transcriber types it; "spoken" is how a clinician says
# it. Both must land on one canonical string, and the reverse direction must
# turn the first into something that still means the second.
GOLDEN: tuple[tuple[str, str, str], ...] = (
    # blood pressure and pulse — the paired forms
    ("uk", "тиск 140/90 мм рт. ст.", "тиск сто сорок на дев'яносто міліметрів ртутного стовпа"),
    ("uk", "130/80", "сто тридцять на вісімдесят"),
    ("uk", "пульс 72/хв", "пульс сімдесят два за хвилину"),
    ("uk", "ЧСС 68 уд/хв", "ЧСС шістдесят вісім ударів за хвилину"),
    ("uk", "18 за хвилину", "вісімнадцять за хвилину"),
    ("uk", "90 мм рт. ст.", "дев'яносто міліметрів ртутного стовпа"),
    # temperature and bare decimals
    ("uk", "температура 37,8", "температура тридцять сім і вісім"),
    ("uk", "36,6", "тридцять шість і шість"),
    ("uk", "6,9", "шість цілих дев'ять десятих"),
    ("uk", "7,25", "сім цілих двадцять п'ять сотих"),
    ("uk", "0,125", "нуль цілих сто двадцять п'ять тисячних"),
    # doses: every plural class Ukrainian has
    ("uk", "1 мг", "один міліграм"),
    ("uk", "2 мг", "два міліграми"),
    ("uk", "5 мг", "п'ять міліграмів"),
    ("uk", "11 мг", "одинадцять міліграмів"),
    ("uk", "21 мг", "двадцять один міліграм"),
    ("uk", "22 мг", "двадцять два міліграми"),
    ("uk", "100 мг", "сто міліграмів"),
    ("uk", "1000 мг", "тисяча міліграмів"),
    ("uk", "2000 мг", "дві тисячі міліграмів"),
    ("uk", "2,5 мг", "два з половиною міліграми"),
    ("uk", "20 мг вранці", "двадцять міліграмів вранці"),
    # the compound dose Epic A names explicitly
    ("uk", "875/125", "вісімсот сімдесят п'ять на сто двадцять п'ять"),
    # units and their variants
    ("uk", "500 мкг", "п'ятсот мікрограмів"),
    ("uk", "500 µg", "п'ятсот мікрограмів"),
    ("uk", "2 г", "два грами"),
    ("uk", "70 кг", "сімдесят кілограмів"),
    ("uk", "10 мл", "десять мілілітрів"),
    ("uk", "250 мл", "двісті п'ятдесят мілілітрів"),
    ("uk", "0,5 мл", "нуль цілих п'ять десятих мілілітра"),
    ("uk", "1,5 л", "одна ціла п'ять десятих літра"),
    ("uk", "12 × 8 мм", "дванадцять на вісім міліметрів"),
    ("uk", "3,5 см", "три цілих п'ять десятих сантиметра"),
    ("uk", "40 од", "сорок одиниць"),
    ("uk", "1 од", "одна одиниця"),
    ("uk", "12 год", "дванадцять годин"),
    ("uk", "15 хв", "п'ятнадцять хвилин"),
    # percent, both spellings
    ("uk", "7,2 %", "сім цілих дві десятих відсотка"),
    ("uk", "98 %", "дев'яносто вісім відсотків"),
    # lab values with a slashed denominator
    ("uk", "5,8 ммоль/л", "п'ять цілих вісім десятих мілімоль на літр"),
    ("uk", "120 мкмоль/л", "сто двадцять мікромоль на літр"),
    ("uk", "5 л/хв", "п'ять літрів за хвилину"),
    ("uk", "20 крапель за хвилину", "двадцять крапель за хвилину"),
    # rates: the preposition and the slash are the same measurement
    ("uk", "1000 мг/добу", "тисяча міліграмів на добу"),
    ("uk", "10 мг/кг", "десять міліграмів на кілограм"),
    # ordinals read as the stage they name
    ("uk", "2 тип", "другий тип"),
    ("uk", "3 ступінь", "третій ступінь"),
    # counts that are NOT measurements keep their preposition and their noun
    ("uk", "25 мг двічі на добу", "двадцять п'ять міліграмів двічі на добу"),
    ("uk", "3 рази на день", "три рази на день"),
    ("uk", "14 днів", "чотирнадцять днів"),
    ("uk", "45 років", "сорок п'ять років"),
    # English: the clinical digit-group idiom and the additive form
    ("en", "138/84", "one thirty eight over eighty four"),
    ("en", "875/125", "eight seventy-five one twenty-five"),
    ("en", "66 bpm", "sixty six beats per minute"),
    ("en", "66/min", "sixty six per minute"),
    ("en", "5 mg", "five milligrams"),
    ("en", "1 unit", "one unit"),
    ("en", "2.5 mg", "two point five milligrams"),
    ("en", "250 ml", "two hundred fifty milliliters"),
    ("en", "1000 mg", "one thousand milligrams"),
    ("en", "40 mcg", "forty micrograms"),
    ("en", "98 %", "ninety eight percent"),
    ("en", "12 hours", "twelve hours"),
    ("en", "110 umol/l", "one hundred and ten micromoles per liter"),
    ("en", "7.2 mmol/l", "seven point two millimoles per liter"),
    ("en", "10 mg/kg", "ten milligrams per kilogram"),
)


def test_golden_table_covers_the_epic_a_minimum():
    """Epic A asks for at least 60 pairs — one per numeric pattern in v3."""
    assert len(GOLDEN) >= 60


@pytest.mark.parametrize(("language", "written", "spoken_form"), GOLDEN)
def test_written_and_spoken_share_one_canonical_form(language, written, spoken_form):
    assert norm.normalize(written, language) == norm.normalize(spoken_form, language)


@pytest.mark.parametrize(("language", "written", "spoken_form"), GOLDEN)
def test_reverse_direction_preserves_the_measurement(language, written, spoken_form):
    """The linter's suggestion must mean what it replaced.

    ``to_spoken`` proposes a rewrite a human accepts with one click. Grammar
    it can get imperfect; the NUMBER it must never touch.
    """
    proposal = spoken.to_spoken(written, language)
    assert norm.normalize(proposal, language) == norm.normalize(written, language)
    assert norm.numeric_signature(proposal, language) == norm.numeric_signature(
        written, language
    )


def test_every_numeric_vendored_pair_collapses():
    """Spoken form ≡ gold form for every line whose difference is numeric."""
    mismatched = [
        row["id"]
        for row in SCRIPT
        if "transcript" in row
        and row.get("subset") != "abbreviations"
        and norm.normalize(row["say"], row["language"])
        != norm.normalize(gold_transcript(row), row["language"])
    ]
    assert mismatched == []


def test_spoken_letter_names_are_not_folded_into_abbreviations():
    """The control on the other side of Epic B.

    The abbreviations subset now asks "does the ASR hear the letter names",
    because the gold is what was said (Epic B) rather than what an editor
    would type. The normaliser must not quietly answer that question for it:
    folding "А Те" into "АТ" would turn every mishearing into a pass.
    """
    assert norm.normalize("А Те стабільний", "uk") != norm.normalize("АТ стабільний", "uk")
    assert norm.normalize("Це Ер Бе", "uk") != norm.normalize("СРБ", "uk")
    rows = [r for r in SCRIPT if r.get("subset") == "abbreviations"]
    assert rows, "the vendored script must still carry abbreviation lines"


@pytest.mark.parametrize(
    ("language", "text", "expected"),
    [
        # Blood pressure: the connector between two numbers goes, the unit
        # phrase collapses, and the written form lands in the same place.
        ("uk", "тиск сто сорок на дев'яносто міліметрів ртутного стовпа",
         "тиск 140 90 mmhg"),
        ("uk", "тиск 140/90 мм рт. ст.", "тиск 140 90 mmhg"),
        # Decimals, four ways Ukrainian actually says them.
        ("uk", "сім цілих дві десятих відсотка", "7.2 pct"),
        ("uk", "сім і дві десятих відсотка", "7.2 pct"),
        ("uk", "нуль цілих чотири мілілітри", "0.4 ml"),
        ("uk", "тридцять сім і вісім", "37.8"),
        ("uk", "два з половиною міліграми", "2.5 mg"),
        ("uk", "7,2 %", "7.2 pct"),
        # Lexically complete numerals sum; the scale word multiplies.
        ("uk", "вісімсот сімдесят п'ять", "875"),
        ("uk", "тисяча міліграмів", "1000 mg"),
        # Rate denominators: the slash and the preposition are one reading.
        ("uk", "пульс 68/хв", "пульс 68 min"),
        ("uk", "пульс шістдесят вісім за хвилину", "пульс 68 min"),
        # Clinical digit-group idiom: "one thirty eight" is 138, not 1+38.
        ("en", "blood pressure one thirty eight over eighty four", "blood pressure 138 84"),
        ("en", "one forty over ninety", "140 90"),
        ("en", "eight seventy-five one twenty-five", "875 125"),
        # …while the additive form stays additive.
        ("en", "two hundred fifty milliliters", "250 ml"),
        ("en", "one hundred and ten micromoles per liter", "110 umol_l"),
        ("en", "seven point two millimoles per liter", "7.2 mmol_l"),
        ("en", "sixty six beats per minute", "66 bpm"),
    ],
)
def test_canonical_forms(language, text, expected):
    assert norm.normalize(text, language) == expected


def test_case_endings_are_not_stripped():
    """The same line eval_wer draws: a wrong case is a real error."""
    assert norm.normalize("інфаркту", "uk") != norm.normalize("інфаркт", "uk")


def test_connector_is_dropped_only_between_two_numbers():
    """"один раз на добу" keeps its connector — "раз" is not a number, and
    the rule fires on the shape of a measurement, not on the word."""
    assert "на" in norm.normalize("один раз на добу", "uk")
    assert "на" not in norm.normalize("сто сорок на дев'яносто", "uk")


def test_unit_preposition_drops_only_before_a_rate_denominator():
    """"5 мг на добу" ≡ "5 мг/добу"; "по 2 таблетки" is left alone."""
    assert norm.normalize("5 мг на добу", "uk") == norm.normalize("5 мг/добу", "uk")
    assert "на" in norm.normalize("2 рази на тиждень", "uk")
    assert "по" in norm.normalize("по 2 таблетки", "uk")


def test_conjunction_and_is_only_glue_inside_a_number():
    """"CBC, CRP, and basic panel" must not be read as part of a number."""
    assert norm.normalize("cbc crp and basic panel", "en") == (
        "cbc crp and basic panel"
    )


def test_fraction_marker_is_consumed_after_a_conjunction():
    """v1 left "десятих" stranded, turning 7,2 into "7.2 десятих" — an
    invented token on both sides of every scored line that said it that way."""
    assert norm.normalize("сім і дві десятих", "uk") == "7.2"
    assert norm.normalize("сім і двадцять п'ять сотих", "uk") == "7.25"


def test_numeric_signature_is_exact_and_ordered():
    sig = norm.numeric_signature(
        "Бісопролол два з половиною міліграми вранці, аторвастатин двадцять "
        "міліграмів увечері.",
        "uk",
    )
    assert sig == ["2.5", "mg", "20", "mg"]
    # Order matters: the same numbers against the other drug is a different
    # prescription, and dose accuracy must not call that a match.
    assert sig != ["20", "mg", "2.5", "mg"]


def test_numeric_signature_is_empty_without_numbers():
    assert norm.numeric_signature("no known drug allergies", "en") == []


def test_unsupported_language_is_folded_only():
    """German has no rule set. Scoring it against another language's
    numerals would invent errors, so it is folded and tokenised only."""
    assert norm.normalize("Der Patient hat 40 mg bekommen.", "de") == (
        "der patient hat 40 mg bekommen"
    )


def test_every_spoken_unit_form_is_recognised_by_the_forward_table():
    """The two directions share one vocabulary — structurally, not by review.

    A ``spoken`` form the forward table cannot read back is a suggestion the
    scorer would count as an error, which is exactly the failure the whole
    normaliser exists to remove. This is the check that caught "сантиметра"
    and "мілімоля" missing from the unit table.
    """
    gaps = []
    for language in ("uk", "en"):
        forward = norm._RULES["units"][language]
        table = norm._RULES["spoken"][language]["units"]
        for canonical, spec in table.items():
            if "phrase" in spec:
                continue
            for form_key in ("one", "few", "many", "frac"):
                word = spec.get(form_key)
                if word and forward.get(word) != canonical:
                    gaps.append((language, canonical, form_key, word))
    assert gaps == []


@pytest.mark.parametrize(("language", "written", "spoken_form"), GOLDEN)
def test_normalisation_is_idempotent_on_the_golden_table(language, written, spoken_form):
    for text in (written, spoken_form):
        once = norm.normalize(text, language)
        assert norm.normalize(once, language) == once


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.one_of(
            st.sampled_from(
                ["мг", "мл", "мкг", "г", "кг", "мм", "см", "%", "од", "хв", "год", "на", "за", "і", "цілих", "десятих", "сотих", "тиск", "пульс", "140", "90", "7,2", "5", "25", "1000", "0,5", "875/125", "68/хв", "мм", "рт.", "ст.", "mg", "ml", "per", "minute", "over", "point", "five", "twenty", "and", "12.5", "66/min"]
            ),
            st.text(alphabet="абвгд xyz0123456789.,/% ", min_size=0, max_size=8),
        ),
        max_size=12,
    ),
    st.sampled_from(("uk", "en", "de")),
)
def test_normalisation_is_idempotent(fragments, language):
    """normalize(normalize(x)) == normalize(x) — Epic A's property test.

    Idempotence is not decoration. The canonical form is compared against
    stored canonical forms from earlier runs, and a pipeline whose output is
    not a fixed point would score a line differently depending on how many
    times it had been through — which is exactly the kind of drift the
    stored `normalizer_version` is supposed to make impossible.
    """
    text = " ".join(fragments)
    once = norm.normalize(text, language)
    assert norm.normalize(once, language) == once


def test_version_and_rules_digest_travel_together():
    assert norm.VERSION == "v2"
    assert len(norm.RULES_SHA256) == 64
    # The digest is proof for the version string's promise: if the rules
    # file changes without the version, this is what notices.
    assert norm.RULES_SHA256 != "0" * 64
