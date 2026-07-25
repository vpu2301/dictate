"""Labeled uk utterance corpus for choice/multi_choice extraction.

The clinical-safety contract in data form. Each case is
``(utterance, expected_value_or_None, why)`` — ``None`` means the
extractor MUST leave the field empty (prose preserved). Every negation
case is a case where filling the field would invert the clinical
meaning of the note.

Options mirror the shipped ``anamnesis_intake`` system template
(`infra/seeds/templates/anamnesis_intake.json`) — if that template's
aliases change, these expectations are the contract that notices.
"""

from __future__ import annotations

from nlp_service.pipeline.base import ChoiceOption

SMOKING_OPTIONS: tuple[ChoiceOption, ...] = (
    ChoiceOption(
        value="never",
        label="не палить",
        aliases=(
            "не палить",
            "не курить",
            "ніколи не палив",
            "ніколи не палила",
            "ніколи не курив",
            "ніколи не курила",
            "заперечує куріння",
            "never smoked",
            "non-smoker",
        ),
    ),
    ChoiceOption(
        value="current",
        label="палить",
        aliases=(
            "палить",
            "курить",
            "активний курець",
            "продовжує палити",
            "продовжує курити",
            "current smoker",
            "smoker",
        ),
    ),
    ChoiceOption(
        value="former",
        label="палив у минулому",
        aliases=(
            "кинув палити",
            "кинула палити",
            "кинув курити",
            "кинула курити",
            "палив у минулому",
            "курив у минулому",
            "колишній курець",
            "раніше палив",
            "раніше курив",
            "ex-smoker",
            "former smoker",
        ),
    ),
)

ALLERGY_OPTIONS: tuple[ChoiceOption, ...] = (
    ChoiceOption(
        value="none_known",
        label="алергії не відомі",
        aliases=(
            "алергії не відомі",
            "без алергій",
            "алергій немає",
            "алергоанамнез не обтяжений",
            "no known allergies",
        ),
    ),
    ChoiceOption(
        value="penicillin",
        label="пеніцилін",
        aliases=("пеніцилін", "амоксицилін", "penicillin"),
    ),
    ChoiceOption(
        value="nsaids",
        label="нпзп",
        aliases=("нпзп", "нестероїдні протизапальні", "аспірин", "ібупрофен", "nsaids"),
    ),
    ChoiceOption(
        value="latex",
        label="латекс",
        aliases=("латекс", "latex"),
    ),
    ChoiceOption(
        value="pollen",
        label="пилок",
        aliases=("пилок", "поліноз", "сезонна алергія", "pollen"),
    ),
)

# ── choice: smoking status ──────────────────────────────────────────
# (utterance, expected value or None, why)
SMOKING_CASES: list[tuple[str, str | None, str]] = [
    # -- straightforward fills
    ("пацієнт курить", "current", "bare alias, exact"),
    ("пацієнт палить", "current", "bare alias, exact"),
    ("хворий активний курець", "current", "multi-token alias"),
    ("пацієнт продовжує палити", "current", "multi-token alias"),
    ("пацієнт не курить", "never", "negated alias is its own option"),
    ("пацієнт не палить", "never", "negated alias is its own option"),
    ("ніколи не палив", "never", "long negated alias"),
    ("заперечує куріння", "never", "clinical phrasing"),
    ("кинув палити п'ять років тому", "former", "alias + trailing detail"),
    ("кинула курити торік", "former", "gendered form"),
    ("колишній курець", "former", "noun phrase"),
    ("раніше палив, зараз ні", "former", "alias with trailing clause"),
    # -- inflection tolerance (Levenshtein ≤ 1 on long tokens)
    ("пацієнт курит", "current", "one-char truncation of курить"),
    ("хворий палит", "current", "one-char truncation of палить"),
    ("заперечує куріння", "never", "one-char variant of куріння"),
    ("кинув палити", "former", "exact multi-token"),
    # -- nothing said about smoking
    ("скарги на головний біль протягом трьох днів", None, "unrelated content"),
    ("", None, "empty text"),
    ("артеріальний тиск сто сорок на дев'яносто", None, "unrelated clinical content"),
    # -- garbled beyond tolerance
    ("пацієнт кржтв", None, "garble, distance > 1"),
    ("пацієнт пллт", None, "garble, distance > 1"),
    # -- negation guard: the safety core
    ("не курець", None, "negator before a non-negated alias blocks it"),
    ("без ознак куріння", None, "'без' blocks"),
    ("заперечує палить", None, "negator token before bare alias blocks"),
]

# ── multi_choice: allergies ─────────────────────────────────────────
# (utterance, expected set or None, why)
ALLERGY_CASES: list[tuple[str, set[str] | None, str]] = [
    ("алергія на пеніцилін", {"penicillin"}, "single allergen"),
    (
        "алергія на пеніцилін, латекс та пилок",
        {"penicillin", "latex", "pollen"},
        "three allergens in one utterance",
    ),
    ("алергії не відомі", {"none_known"}, "explicit none"),
    ("алергоанамнез не обтяжений", {"none_known"}, "clinical phrasing for none"),
    (
        "алергій немає, окрім латексу",
        {"latex"},
        "none_known dropped when a positive finding is also present",
    ),
    ("непереносимість аспірину", {"nsaids"}, "alias of the NSAID group"),
    ("реакція на амоксицилін", {"penicillin"}, "drug-name alias maps to its group"),
    ("скарги на кашель", None, "nothing allergy-related"),
    ("", None, "empty text"),
]
