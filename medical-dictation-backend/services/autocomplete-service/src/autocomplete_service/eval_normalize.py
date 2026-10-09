"""Normalised WER — the second, clinically-honest reading of the same audio.

Raw WER counts "сорок міліграмів" against a gold "40 мг" as two errors. The
model heard the dose correctly; the scorer disagreed with the transcriber's
house style. Corpus-v2 §1.1 names this directly: part of the 40.7% on the
numbers subset is a scoring artefact, not a recognition failure, and nobody
can tell which part without a second number to compare against.

So every utterance is scored TWICE and both are stored: raw (what the
tokeniser sees) and normalised (what a clinician would call the same
utterance). Neither replaces the other —

  · raw alone over-reports numeric error and hides real drug-name error
    behind it;
  · normalised alone forgives formatting the report generator does have to
    get right, and can be tuned into meaninglessness by adding rules.

Reporting them side by side makes the gap itself the interesting number:
a large gap means the ASR heard correctly and the writing convention
differs; a small gap means the model genuinely misheard.

WHAT NORMALISATION DOES (rules in data/eval_normalization_v2.yaml):
  lowercase → fold apostrophes/dashes/slashes → decimal comma to point →
  strip punctuation → collapse multi-word units ("мілімоль на літр" →
  mmol_l) → number words to digits, both languages, decimals and clinical
  idiom included ("сто сорок на дев'яносто" → "140 90", "one thirty eight"
  → "138") → drop the connector between two numbers → drop the preposition
  in front of a rate denominator ("68 за хвилину" ≡ "68/хв") →
  canonicalise units ("міліграмів"/"мг"/"mg" → mg).

THE REVERSE DIRECTION lives in ``eval_spoken``: same rules file, digits to
words, feeding the gold-transcript linter (corpus-v3 Epic B). Keeping both
directions on one table is what makes "the linter proposes what the scorer
accepts" true by construction rather than by review.

WHAT IT DELIBERATELY DOES NOT DO: stem, lemmatise or strip Ukrainian case
endings. "інфаркту" vs "інфаркт" is a real error and must count — the same
line eval_wer draws, for the same reason.

THE RULES ARE DATA AND THE VERSION IS STORED. A rules change silently makes
new numbers incomparable with old ones, so ``VERSION`` travels onto every
run row and the comparison endpoint refuses runs that disagree about it.

Stdlib only, deterministic, no I/O beyond loading its own rules file — so
``scripts/eval/wer_lib.py`` imports it directly rather than growing a second
copy to drift against (the duplication ``eval_wer`` carries exists because
the arithmetic predates this module; there is no reason to repeat it).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Final

import yaml

_RULES_PATH: Final = Path(__file__).parent / "data" / "eval_normalization_v2.yaml"
_RULES_BYTES: Final = _RULES_PATH.read_bytes()
_RULES: Final[dict[str, Any]] = yaml.safe_load(_RULES_BYTES.decode("utf-8"))

#: Stored on every run. Changing the rules file MUST change this string.
VERSION: Final[str] = str(_RULES["version"])
#: Digest of the rules file — the version string is a promise, this is proof.
RULES_SHA256: Final[str] = hashlib.sha256(_RULES_BYTES).hexdigest()

#: Languages with a rule set. Anything else is folded and tokenised only —
#: never silently scored against another language's numerals.
LANGUAGES: Final[tuple[str, ...]] = ("uk", "en")

_HALLUCINATION_STOPLIST: Final[tuple[str, ...]] = tuple(
    _RULES["hallucination_stoplist"]
)

_APOSTROPHES: Final = _RULES["fold"]["apostrophes"]
_TO_SPACE: Final = _RULES["fold"]["to_space"]
_PCT: Final[str] = _RULES["fold"]["percent_token"]

# Everything the unit passes can produce — the vocabulary dose accuracy
# treats as "part of the number" rather than as a word.
CANONICAL_UNITS: Final[frozenset[str]] = frozenset(
    [v for table in _RULES["units"].values() for v in table.values()]
    + [to for table in _RULES["phrases"].values() for _, to in table]
)

_DIGITS = re.compile(r"^\d+(?:\.\d+)?$")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")
_DECIMAL_DOT = re.compile(r"(?<=\d)\.(?=\d)")
# Same class as eval_wer.tokenize: Unicode word chars plus the apostrophe,
# so Cyrillic and "дев'яносто" both survive.
_NON_TOKEN = re.compile(r"[^\w'\x00]+", flags=re.UNICODE)

_DOT_SENTINEL: Final = "\x00"


def _rules(section: str, language: str) -> Any:
    return _RULES[section].get(language, _RULES[section].get("en"))


# ── stage 0: folding and tokenisation ──────────────────────────────────


def fold(text: str) -> str:
    """Case, apostrophe, separator and percent folding.

    ``/`` and ``×`` become spaces because they are how a transcriber writes
    what a clinician says as a word: "140/90" and "12 × 8" are read aloud as
    "сто сорок НА дев'яносто" and "дванадцять НА вісім", and the connector
    is dropped later on the digit stream. Hyphens split for the same reason
    ("eighty-eight" is two number words) — harmlessly, since both sides of
    the comparison are folded identically.
    """
    s = text.lower()
    for ch in _APOSTROPHES:
        s = s.replace(ch, "'")
    for ch in _TO_SPACE:
        s = s.replace(ch, " ")
    s = s.replace("%", f" {_PCT} ")
    # "7,2" is one number; the comma in "стабільний, ЧСС" is punctuation.
    s = _DECIMAL_COMMA.sub(".", s)
    # Protect the decimal point before punctuation stripping eats it.
    s = _DECIMAL_DOT.sub(_DOT_SENTINEL, s)
    s = _NON_TOKEN.sub(" ", s)
    return s.replace(_DOT_SENTINEL, ".")


def _tokenize(text: str) -> list[str]:
    return fold(text).split()


# ── stage 1: multi-word units ──────────────────────────────────────────


def _apply_phrases(tokens: list[str], language: str) -> list[str]:
    """Longest-match multi-word unit collapse, BEFORE number handling.

    Order matters: "мілімоль НА літр" contains the same connector the digit
    pass later drops, and collapsing the unit first is what keeps the
    connector rule from having to know about chemistry.
    """
    table = _rules("phrases", language)
    if not table:
        return tokens
    by_len: dict[int, dict[tuple[str, ...], str]] = {}
    for words, to in table:
        by_len.setdefault(len(words), {})[tuple(words)] = to
    lengths = sorted(by_len, reverse=True)

    out: list[str] = []
    i = 0
    while i < len(tokens):
        for n in lengths:
            key = tuple(tokens[i : i + n])
            if len(key) == n and key in by_len[n]:
                out.append(by_len[n][key])
                i += n
                break
        else:
            out.append(tokens[i])
            i += 1
    return out


# ── stage 2: number words → digits ─────────────────────────────────────


def _fmt(value: float | int) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return f"{value:.10f}".rstrip("0").rstrip(".")


def _read_run(tokens: list[str], start: int, language: str) -> int:
    """End index of the maximal number-word run beginning at ``start``.

    ``glue`` ("and" in "one hundred AND ten") only extends a run when a
    number word follows it — otherwise "CBC, CRP, and basic panel" would
    swallow the conjunction and "and a half" could never be recognised.
    """
    cardinals = _rules("cardinals", language)
    multipliers = _rules("multipliers", language)
    scales = _rules("scales", language)
    glue = _rules("number_glue", language)

    def is_number_word(word: str) -> bool:
        return word in cardinals or word in multipliers or word in scales

    i = start
    while i < len(tokens):
        word = tokens[i]
        if is_number_word(word):
            i += 1
            continue
        if (
            word in glue
            and i + 1 < len(tokens)
            and is_number_word(tokens[i + 1])
            and i > start
        ):
            i += 1
            continue
        break
    return i


def _value_generic(words: list[str], language: str) -> int:
    """Additive accumulation with multipliers and scales.

    Ukrainian numerals are lexically complete ("вісімсот" is 800), so the sum
    is the whole story. English needs the multiplier step: "two hundred
    fifty" is 2×100 + 50, and a bare "hundred" means one hundred.
    """
    cardinals = _rules("cardinals", language)
    multipliers = _rules("multipliers", language)
    scales = _rules("scales", language)

    total = 0
    current = 0
    seen = False
    for word in words:
        if word in cardinals:
            current += int(cardinals[word])
            seen = True
        elif word in multipliers:
            current = (current or 1) * int(multipliers[word])
            seen = True
        elif word in scales:
            total += (current or 1) * int(scales[word])
            current = 0
            seen = True
    return (total + current) if seen else 0


def _render_run(words: list[str], language: str) -> list[str]:
    if language == "en":
        cardinals = _rules("cardinals", "en")
        if all(w in cardinals for w in words):
            groups: list[list[str]] = []
            for word in words:
                value = int(cardinals[word])
                is_unit = 1 <= value <= 9
                is_tens = 20 <= value <= 90 and value % 10 == 0
                if not groups:
                    groups.append([word])
                    continue
                group = groups[-1]
                head = int(cardinals[group[0]])
                if is_tens and len(group) == 1 and 1 <= head <= 9 or is_unit and len(group) == 2 and int(cardinals[group[1]]) >= 20 or is_unit and len(group) == 1 and 20 <= head <= 90:
                    group.append(word)
                else:
                    groups.append([word])
            rendered: list[str] = []
            for group in groups:
                head = int(cardinals[group[0]])
                if len(group) >= 2 and 1 <= head <= 9:
                    rendered.append(
                        _fmt(head * 100 + sum(int(cardinals[w]) for w in group[1:]))
                    )
                else:
                    rendered.append(_fmt(sum(int(cardinals[w]) for w in group)))
            return rendered
    return [_fmt(_value_generic(words, language))]


def _words_to_digits(tokens: list[str], language: str) -> list[str]:
    ordinals = _rules("ordinals", language)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        end = _read_run(tokens, i, language)
        if end > i:
            out.extend(_render_run(tokens[i:end], language))
            i = end
            continue
        word = tokens[i]
        if word in ordinals:
            # "другого типу" ≡ "2 типу". This conflates the ordinal with the
            # cardinal ("три стадії" also becomes "3 стадії") — accepted:
            # both name the same stage, and the alternative is scoring a
            # correct reading as an error over a suffix.
            out.append(_fmt(int(ordinals[word])))
        else:
            out.append(word)
        i += 1
    return out


# ── stage 3: decimals and halves ───────────────────────────────────────


def _is_number(token: str) -> bool:
    return bool(_DIGITS.match(token))


def _join_decimal(whole: str, frac_digits: str) -> str:
    return _fmt(float(f"{int(float(whole))}.{frac_digits}"))


def _apply_decimals_uk(tokens: list[str]) -> list[str]:
    rules = _rules("decimals", "uk")
    whole_markers = set(rules["whole_markers"])
    fraction_markers = rules["fraction_markers"]
    conjunctions = set(rules["conjunctions"])
    half_words = [tuple(h) for h in rules["half_words"]]

    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        # "<A> цілих <B> [десятих]"  — "нуль цілих чотири" has no marker,
        # which is how it is actually said; the fraction width then comes
        # from the digits themselves.
        if (
            _is_number(token)
            and i + 2 < len(tokens)
            and tokens[i + 1] in whole_markers
            and _is_number(tokens[i + 2])
        ):
            frac = tokens[i + 2]
            consumed = 3
            marker = tokens[i + 3] if i + 3 < len(tokens) else None
            width = fraction_markers.get(marker) if marker else None
            if width is not None:
                consumed = 4
                frac = frac.zfill(int(width))
            out.append(_join_decimal(token, frac))
            i += consumed
            continue
        # "<A> і <B>" — "тридцять сім і вісім" is 37,8 on the page. Gated to
        # a single-digit right side so a genuine conjunction between two
        # quantities ("сто і двісті") is left alone — UNLESS an explicit
        # fraction marker follows, which settles the reading on its own:
        # "сім і двадцять п'ять сотих" is 7,25 and nothing else. Consuming
        # that marker is the point; leaving it stranded is how v1 turned
        # "сім і дві десятих" into "7.2 десятих".
        if (
            _is_number(token)
            and i + 2 < len(tokens)
            and tokens[i + 1] in conjunctions
            and _is_number(tokens[i + 2])
        ):
            marker = tokens[i + 3] if i + 3 < len(tokens) else None
            width = fraction_markers.get(marker) if marker else None
            if width is not None:
                out.append(_join_decimal(token, tokens[i + 2].zfill(int(width))))
                i += 4
                continue
            if len(tokens[i + 2]) == 1:
                out.append(_join_decimal(token, tokens[i + 2]))
                i += 3
                continue
        if _is_number(token):
            matched = False
            for half in half_words:
                if tuple(tokens[i + 1 : i + 1 + len(half)]) == half:
                    out.append(_fmt(float(token) + 0.5))
                    i += 1 + len(half)
                    matched = True
                    break
            if matched:
                continue
        out.append(token)
        i += 1
    return out


def _apply_decimals_en(tokens: list[str]) -> list[str]:
    rules = _rules("decimals", "en")
    point_words = set(rules["point_words"])
    half_words = [tuple(h) for h in rules["half_words"]]

    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if (
            _is_number(token)
            and i + 2 < len(tokens)
            and tokens[i + 1] in point_words
            and _is_number(tokens[i + 2])
        ):
            # "point four five" is one fraction spoken digit by digit.
            frac = ""
            j = i + 2
            while j < len(tokens) and _is_number(tokens[j]) and len(tokens[j]) == 1:
                frac += tokens[j]
                j += 1
            if not frac:
                frac = tokens[i + 2]
                j = i + 3
            out.append(_join_decimal(token, frac))
            i = j
            continue
        if _is_number(token):
            matched = False
            for half in half_words:
                if tuple(tokens[i + 1 : i + 1 + len(half)]) == half:
                    out.append(_fmt(float(token) + 0.5))
                    i += 1 + len(half)
                    matched = True
                    break
            if matched:
                continue
        out.append(token)
        i += 1
    return out


# ── stage 4: connectors and single-word units ──────────────────────────


def _drop_connectors(tokens: list[str], language: str) -> list[str]:
    """"140 на 90" → "140 90". Only between two numbers.

    "один раз НА добу" keeps its connector because "раз" is not a number —
    which is the whole guard: the rule fires on the shape of a measurement,
    not on the word.
    """
    connectors = set(_rules("connectors", language))
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if (
            0 < i < len(tokens) - 1
            and tokens[i] in connectors
            and _is_number(tokens[i - 1])
            and _is_number(tokens[i + 1])
        ):
            i += 1
            continue
        out.append(tokens[i])
        i += 1
    return out


def _drop_unit_prepositions(tokens: list[str], language: str) -> list[str]:
    """"68 ЗА хвилину" → "68 хвилину" ≡ "68/хв".

    A transcriber writing "68/хв" and a clinician saying "шістдесят вісім за
    хвилину" mean one measurement. Folding turned the slash into a space
    already; what was left over was the preposition, and that single token
    was two thirds of the 50% WER on uk-cardiology-a001 (Epic A's opening
    complaint).

    The rule is gated on BOTH sides, not on the preposition:

      · the left neighbour must be a number or a unit — "один раз НА добу"
        keeps its preposition because "раз" is neither, and both the spoken
        and the written form say it anyway;
      · the right neighbour must be a rate denominator — a closed list of
        the nouns that appear under a clinical slash (хв, год, добу, кг, л).
        "по 2 таблетки" is untouched: "таблетки" is not one.

    Runs BEFORE unit canonicalisation so both spellings of the denominator
    ("хв" from the slash, "хвилину" from the speech) are still themselves;
    the unit pass then maps both onto `min`.
    """
    prepositions = set(_rules("unit_prepositions", language) or ())
    denominators = set(_rules("rate_denominators", language) or ())
    if not prepositions or not denominators:
        return tokens
    unit_table = _rules("units", language)
    quantity_left = set(unit_table) | set(unit_table.values()) | CANONICAL_UNITS

    out: list[str] = []
    i = 0
    while i < len(tokens):
        if (
            0 < i < len(tokens) - 1
            and tokens[i] in prepositions
            and tokens[i + 1] in denominators
            and (_is_number(tokens[i - 1]) or tokens[i - 1] in quantity_left)
        ):
            i += 1
            continue
        out.append(tokens[i])
        i += 1
    return out


def _apply_units(tokens: list[str], language: str) -> list[str]:
    table = _rules("units", language)
    return [table.get(t, t) for t in tokens]


# ── public API ─────────────────────────────────────────────────────────


def normalize_tokens(text: str, language: str) -> list[str]:
    """The normalised token sequence — the input to normalised WER."""
    tokens = _tokenize(text)
    if language not in LANGUAGES:
        # No rule set: fold and tokenise only. Scoring an unsupported
        # language against another one's numerals would invent errors.
        return tokens
    tokens = _apply_phrases(tokens, language)
    tokens = _words_to_digits(tokens, language)
    tokens = _apply_decimals_uk(tokens) if language == "uk" else _apply_decimals_en(tokens)
    tokens = _drop_connectors(tokens, language)
    tokens = _drop_unit_prepositions(tokens, language)
    tokens = _apply_units(tokens, language)
    return tokens


def normalize(text: str, language: str) -> str:
    return " ".join(normalize_tokens(text, language))


def numeric_signature(text: str, language: str) -> list[str]:
    """Every number and unit, in order — the dose-accuracy comparand.

    Corpus-v2 §1.3.2 wants a metric that answers "did the model get the
    dose right", not "how close was it on average". A dose is right or it is
    a different dose, so this is compared for exact equality: 40 mg twice
    daily and 4 mg twice daily differ by one character and by a factor of
    ten.
    """
    return [
        t for t in normalize_tokens(text, language)
        if _is_number(t) or t in CANONICAL_UNITS
    ]


def hallucination_stoplist() -> tuple[str, ...]:
    """Known ASR-on-silence outputs, folded for substring matching."""
    return tuple(fold(p).strip() for p in _HALLUCINATION_STOPLIST)
