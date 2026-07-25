"""numeric_with_unit / date binders (sprint 13, step 05).

These are **binders, not parsers**. They pick from the artifacts the
sprint-05 normalizer stages reported about their own output; they
contain no numeral vocabulary, no unit table and no date arithmetic of
their own. That is the single-source rule: if a binder re-derived
"сто сорок" → 140 or resolved "три дні тому", it would drift from the
normalizer the first time either changed.

The ambiguity rule from step 04 applies unchanged: several candidates
and no way to choose ⇒ empty field, prose preserved.
"""

from __future__ import annotations

from typing import Final

from report_models import DateMeta, NumericMeta

from ...pipeline.base import DateArtifact, NumericArtifact, TemplateSection
from .choice import tokenize

# Confidence constants — pilot-tunable, documented in ADR-0032.
#
# LABELLED sits above the 0.8 threshold: the section's own name or an
# alias appeared next to the value, which is real evidence of intent.
LABELLED_CONFIDENCE: Final = 0.9
# SOLE sits EXACTLY at the threshold so a clean single-measurement
# section binds. Anything lower would make the common case unfillable;
# anything higher would claim evidence we do not have.
SOLE_CONFIDENCE: Final = 0.8
# A date is a stronger signal than a bare number: the normalizer only
# emits ISO forms it actually recognised as dates.
DATE_CONFIDENCE: Final = 0.9

# How many tokens around a value are scanned for the section's label.
LABEL_WINDOW: Final = 4


def _label_tokens(section: TemplateSection) -> set[str]:
    """The words that would mark a value as belonging to this section."""
    tokens: set[str] = set(tokenize(section.name))
    for alias in section.aliases:
        tokens.update(tokenize(alias))
    # Very short tokens are noise as labels ("на", "і").
    return {t for t in tokens if len(t) >= 4}


def _label_distance(text: str, artifact: NumericArtifact, labels: set[str]) -> int | None:
    """Token distance from the value to its NEAREST section label.

    A distance rather than a boolean: in "маса 80 кг, температура 37,2"
    both values sit within a few tokens of "температура", so a boolean
    window would call both labelled and give up. Nearest-wins picks the
    value the clinician actually attached to the label, and an exact tie
    still falls through to the ambiguity rule.
    """
    if not labels:
        return None
    tokens = tokenize(text)
    value_tokens = tokenize(artifact.value)
    if not value_tokens:
        return None
    positions = [i for i, t in enumerate(tokens) if t == value_tokens[0]]
    if not positions:
        return None
    best: int | None = None
    for position in positions:
        for index, token in enumerate(tokens):
            if index == position or token not in labels:
                continue
            distance = abs(index - position)
            if distance <= LABEL_WINDOW and (best is None or distance < best):
                best = distance
    return best


def bind_numeric(
    text: str,
    artifacts: tuple[NumericArtifact, ...],
    section: TemplateSection,
    *,
    threshold: float,
) -> NumericMeta | None:
    """Bind one measurement to a ``numeric_with_unit`` section.

    Rules, in order:

    1. *(dormant)* match the section's declared expected unit — as-built
       ``TemplateSection`` carries no unit hint, so this rule cannot
       fire yet. When a template gains one, it slots in here as the
       strongest signal.
    2. A value labelled by the section's name/alias tokens nearby ⇒
       ``LABELLED_CONFIDENCE``.
    3. Exactly one unit-bearing value in the section ⇒
       ``SOLE_CONFIDENCE``.

    Several unlabelled candidates ⇒ empty (ambiguity). A value with no
    unit ⇒ empty: a ``numeric_with_unit`` field without a unit is not
    a measurement, and guessing the unit is exactly the kind of
    clinical fabrication this sprint refuses.
    """
    with_units = [a for a in artifacts if a.unit]
    if not with_units:
        return None

    labels = _label_tokens(section)
    scored = [(a, _label_distance(text, a, labels)) for a in with_units]
    labelled = sorted(((a, d) for a, d in scored if d is not None), key=lambda pair: pair[1])

    if labelled and (len(labelled) == 1 or labelled[0][1] < labelled[1][1]):
        chosen, confidence = labelled[0][0], LABELLED_CONFIDENCE
    elif labelled:
        return None  # two labels equidistant — genuinely ambiguous
    elif len(with_units) == 1:
        chosen, confidence = with_units[0], SOLE_CONFIDENCE
    else:
        return None  # several unlabelled candidates

    if confidence < threshold:
        return None
    return NumericMeta(
        value=float(chosen.value.replace(",", ".")),
        unit=chosen.unit,
        confidence=confidence,
        source="extracted",
    )


def bind_date(
    artifacts: tuple[DateArtifact, ...],
    *,
    threshold: float,
) -> DateMeta | None:
    """Bind one ISO date to a ``date`` / ``date_with_note`` section.

    Exactly one date ⇒ bound. Several ⇒ empty: which one the clinician
    meant is not inferable, and picking the first would silently
    misdate a clinical record.

    ``date_with_note`` needs no note extraction — the dictated prose IS
    the note and already lives in the section's ``text``.

    Relative dates ("три дні тому") appear here only if the sprint-05
    date normalizer resolved them; this binder never resolves anything.
    """
    if len(artifacts) != 1:
        return None
    if threshold > DATE_CONFIDENCE:
        return None
    return DateMeta(
        date=artifacts[0].iso,
        confidence=DATE_CONFIDENCE,
        source="extracted",
    )
