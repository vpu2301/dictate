"""choice / multi_choice extraction (sprint 13, step 04).

**Prime directive: the extractor proposes, the clinician confirms.**
Below threshold, ambiguous, or negated ⇒ NO selection. An empty field
with the prose preserved is always correct; a wrong auto-filled
clinical field is not.

Pure functions — no I/O, no clock, no randomness, injected thresholds.
Every decision here is replayable byte-for-byte, which is why the
edit-distance implementation is local (below) rather than a library
call: a dependency upgrade that changed edit-distance edge semantics
would silently alter historical replays under a frozen
``pipeline_version``.

Matching, per option, per candidate phrase (its label + each alias):

1. Tokenize text and phrase identically (NFC, lower, split on
   non-alphanumerics, apostrophes folded).
2. Slide the phrase over the text tokens. A phrase matches at a
   position when EVERY phrase token is within Levenshtein 1 of the
   aligned text token — and tokens of ≤ 3 characters must match
   EXACTLY (the short-token guard; without it "не" fuzzy-matches "ні"
   and "на", which would invert the meaning of a Ukrainian clinical
   statement).
3. A negator in the 2 tokens before the match blocks it, unless the
   negator is part of the matched phrase itself (aliases like
   "не палить" legitimately contain one).
4. Confidence = token tightness (1 − Σdistance/Σlength), weighted so a
   longer phrase match outranks a single-token one; an exact
   full-phrase match scores 1.0.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from report_models import ChoiceMeta, MultiChoiceMeta

from ...pipeline.base import ChoiceOption

# Tokens shorter than this must match exactly — no fuzzy tolerance.
SHORT_TOKEN_EXACT_BELOW: Final = 4
# How many tokens before a match are scanned for a negator.
NEGATION_WINDOW: Final = 2
# Multi-choice convention: an "explicitly nothing" option loses to any
# positive finding. Template authors use this value (see the authoring
# doc) when a section needs an explicit "none" answer.
EXCLUSIVE_NONE_VALUE: Final = "none_known"

NEGATORS: Final[frozenset[str]] = frozenset(
    {
        # uk
        "не",
        "ні",
        "без",
        "заперечує",
        "заперечував",
        "заперечувала",
        "немає",
        "нема",
        "відсутні",
        "відсутній",
        "відсутня",
        # en
        "no",
        "not",
        "never",
        "denies",
        "denied",
        "without",
    }
)

# Contrast markers cancel a preceding negator: in "алергій немає, окрім
# латексу" the negation governs the first clause only, and reading the
# whole utterance as "no allergies" would DROP a latex allergy. Missing a
# real allergy is the most dangerous error this extractor can make, so
# the negation guard stops at these words.
CONTRAST_MARKERS: Final[frozenset[str]] = frozenset(
    {
        # uk
        "окрім",
        "крім",
        "але",
        "проте",
        "однак",
        "лише",
        "тільки",
        # en
        "except",
        "besides",
        "but",
        "however",
        "only",
    }
)

_TOKEN_SPLIT_RE: Final = re.compile(r"[^0-9a-zа-яїієґё']+", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    """NFC + lower + split on non-alphanumerics. Deterministic."""
    normalized = unicodedata.normalize("NFC", text).lower()
    return [t for t in _TOKEN_SPLIT_RE.split(normalized) if t]


def _within_distance(a: str, b: str, max_distance: int) -> int | None:
    """Exact Levenshtein distance if ≤ ``max_distance``, else ``None``.

    Local implementation on purpose — see the module docstring on
    replay determinism. Bounded work: strings differing in length by
    more than ``max_distance`` short-circuit.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_distance:
        return None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        if min(current) > max_distance:
            return None
        previous = current
    distance = previous[-1]
    return distance if distance <= max_distance else None


@dataclass(frozen=True, slots=True)
class PhraseMatch:
    value: str
    confidence: float
    start_token: int
    end_token: int  # exclusive
    phrase_len: int


def _token_distance(text_token: str, phrase_token: str) -> int | None:
    """Distance under the short-token guard, or None if not a match."""
    if len(phrase_token) < SHORT_TOKEN_EXACT_BELOW or len(text_token) < SHORT_TOKEN_EXACT_BELOW:
        return 0 if text_token == phrase_token else None
    return _within_distance(text_token, phrase_token, 1)


def _is_negated(text_tokens: list[str], start: int, phrase_tokens: list[str]) -> bool:
    """True when a negator governs the match.

    Three rules, in order:
    1. A phrase carrying its own negation ("не палить") is never
       self-blocked — otherwise negative options would be unfillable.
    2. A contrast marker between the negator and the match cancels the
       negation (see ``CONTRAST_MARKERS``).
    3. Otherwise, a negator within ``NEGATION_WINDOW`` tokens blocks.
    """
    if any(t in NEGATORS for t in phrase_tokens):
        return False
    window = text_tokens[max(0, start - NEGATION_WINDOW) : start]
    # Walk backwards from the match: the nearest of (negator, contrast
    # marker) decides. A contrast marker shields everything after it.
    for token in reversed(window):
        if token in CONTRAST_MARKERS:
            return False
        if token in NEGATORS:
            return True
    return False


def _match_phrase(text_tokens: list[str], phrase: str, *, value: str) -> PhraseMatch | None:
    """Best (highest-confidence) non-negated match of one phrase."""
    phrase_tokens = tokenize(phrase)
    if not phrase_tokens or len(phrase_tokens) > len(text_tokens):
        return None

    best: PhraseMatch | None = None
    span = len(phrase_tokens)
    for start in range(len(text_tokens) - span + 1):
        total_distance = 0
        total_length = 0
        ok = True
        for offset, phrase_token in enumerate(phrase_tokens):
            distance = _token_distance(text_tokens[start + offset], phrase_token)
            if distance is None:
                ok = False
                break
            total_distance += distance
            total_length += max(len(phrase_token), 1)
        if not ok:
            continue
        if _is_negated(text_tokens, start, phrase_tokens):
            continue

        tightness = 1.0 - (total_distance / total_length)
        # Weight longer phrases up: matching "кинув палити" is stronger
        # evidence than matching "палити" alone. Single-token matches keep
        # their raw tightness; each extra token closes 25% of the gap to 1.
        weight = 1.0 - 0.75 ** (span - 1)
        confidence = tightness + (1.0 - tightness) * weight
        confidence = round(min(1.0, confidence), 6)
        candidate = PhraseMatch(
            value=value,
            confidence=confidence,
            start_token=start,
            end_token=start + span,
            phrase_len=span,
        )
        if best is None or _better(candidate, best):
            best = candidate
    return best


def _better(a: PhraseMatch, b: PhraseMatch) -> bool:
    """Deterministic ordering: confidence, then longer phrase, then
    earlier position. No ties are ever broken by iteration order."""
    return (a.confidence, a.phrase_len, -a.start_token) > (
        b.confidence,
        b.phrase_len,
        -b.start_token,
    )


def _subsume_overlaps(matches: list[PhraseMatch]) -> list[PhraseMatch]:
    """Drop matches whose tokens are already claimed by a longer match.

    "кинув палити" matches ``former`` over two tokens, and its second
    token alone fuzzy-matches ``current``'s "палить". Those are the SAME
    words read two ways — not two competing clinical claims — so the
    longer reading wins and the shorter is discarded. Without this,
    every "quit smoking" utterance would look ambiguous and fill
    nothing.

    Genuinely disjoint evidence (e.g. "палить ... не палить" at
    different positions) keeps both matches, so real contradictions
    still surface as ambiguity.
    """
    # Longest phrase first, then strongest; ties broken deterministically.
    ordered = sorted(
        matches,
        key=lambda m: (-m.phrase_len, -m.confidence, m.start_token, m.value),
    )
    kept: list[PhraseMatch] = []
    claimed: set[int] = set()
    for match in ordered:
        span = set(range(match.start_token, match.end_token))
        if span & claimed:
            continue
        claimed |= span
        kept.append(match)
    return kept


def match_options(text: str, options: tuple[ChoiceOption, ...]) -> list[PhraseMatch]:
    """Best match per option, overlaps subsumed, ordered best-first."""
    text_tokens = tokenize(text)
    if not text_tokens:
        return []

    matches: list[PhraseMatch] = []
    for option in options:
        best: PhraseMatch | None = None
        # Label first, then aliases in template order — but ordering only
        # affects which equally-scored phrase is reported, never the score.
        for phrase in (option.label, *option.aliases):
            found = _match_phrase(text_tokens, phrase, value=option.value)
            if found is not None and (best is None or _better(found, best)):
                best = found
        if best is not None:
            matches.append(best)

    matches = _subsume_overlaps(matches)
    matches.sort(key=lambda m: (-m.confidence, -m.phrase_len, m.start_token, m.value))
    return matches


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """The metadata (or None) plus WHY — the stage reports the reason as
    a metric label so step 08's dashboard can tell "nothing was said"
    from "we heard two conflicting things"."""

    meta: object | None  # a report_models *Meta, or None
    outcome: str  # 'filled' | 'empty' | 'ambiguous'


def choose(
    text: str,
    options: tuple[ChoiceOption, ...],
    *,
    threshold: float,
) -> ExtractionResult:
    """Single-select extraction with its outcome reason.

    Ambiguity rule: if two DIFFERENT options both clear the threshold,
    nothing is selected. Competing clinical signals are not a coin flip.
    """
    above = [m for m in match_options(text, options) if m.confidence >= threshold]
    if not above:
        return ExtractionResult(None, "empty")
    if len({m.value for m in above}) > 1:
        return ExtractionResult(None, "ambiguous")
    best = above[0]
    return ExtractionResult(
        ChoiceMeta(selected=best.value, confidence=best.confidence, source="extracted"),
        "filled",
    )


def choose_multi(
    text: str,
    options: tuple[ChoiceOption, ...],
    *,
    threshold: float,
) -> ExtractionResult:
    """Multi-select extraction with its outcome reason.

    All options clearing the threshold are selected. The exclusive
    "none" convention applies: an explicit ``none_known`` is dropped
    when any positive finding also matched — the clinician said both,
    and the positive finding is the safe reading.
    """
    above = [m for m in match_options(text, options) if m.confidence >= threshold]
    if not above:
        return ExtractionResult(None, "empty")

    selected = [m.value for m in above]
    positives = [v for v in selected if v != EXCLUSIVE_NONE_VALUE]
    if positives and EXCLUSIVE_NONE_VALUE in selected:
        selected = positives

    # Report the weakest member's confidence: the metadata describes the
    # selection as a whole, and a set is only as certain as its least
    # certain member.
    confidence = round(min(m.confidence for m in above if m.value in selected), 6)
    return ExtractionResult(
        MultiChoiceMeta(
            selected=tuple(selected),
            confidence=confidence,
            source="extracted",
        ),
        "filled",
    )


def extract_choice(
    text: str,
    options: tuple[ChoiceOption, ...],
    *,
    threshold: float,
) -> ChoiceMeta | None:
    """Single-select extraction. ``None`` ⇒ leave the field empty."""
    meta = choose(text, options, threshold=threshold).meta
    assert meta is None or isinstance(meta, ChoiceMeta)
    return meta


def extract_multi_choice(
    text: str,
    options: tuple[ChoiceOption, ...],
    *,
    threshold: float,
) -> MultiChoiceMeta | None:
    """Multi-select extraction. ``None`` ⇒ leave the field empty."""
    meta = choose_multi(text, options, threshold=threshold).meta
    assert meta is None or isinstance(meta, MultiChoiceMeta)
    return meta
