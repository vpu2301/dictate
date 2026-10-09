"""The reverse direction — digits back into the words a clinician says.

``eval_normalize`` folds "сорок міліграмів" and "40 мг" onto one canonical
form so the scorer stops counting house style as a recognition error. This
module walks the same table the other way, and it exists for one job:
corpus-v3 Epic B says gold transcripts are written in SPOKEN form, and a
rule nobody can comply with cheaply is a rule that gets ignored. So when the
linter sees a gold reading "пульс 68/хв" it does not merely complain — it
offers "пульс шістдесят вісім за хвилину" and lets the author take it.

WHY THIS IS A SUGGESTION AND NOT A REWRITE. Ukrainian numeral–noun agreement
has more cases than a table settles: gender ("одна крапля" but "один
міліграм"), the paucal 2–4 ("два міліграми"), the genitive after a decimal
("сім цілих дві десятих ВІДСОТКА"), and animacy questions this domain never
raises but the language still has. The tables in
``data/eval_normalization_v2.yaml`` cover clinical dictation and stop there.
Every caller treats the output as a proposal a human confirms — nothing in
this module writes to the corpus.

WHAT MAKES IT TRUSTWORTHY ANYWAY: the round-trip property. For every golden
fixture, ``normalize(to_spoken(x)) == normalize(x)`` — the suggestion means
the same measurement as the text it replaces, or the test fails. That is a
weaker claim than "the grammar is right" and a much more useful one: a
suggestion that changed the dose would be a clinical-safety bug, and this is
the assertion that makes it impossible.

Stdlib plus the shared rules file. No I/O, deterministic.
"""

from __future__ import annotations

import re
from typing import Any, Final

from .eval_normalize import _RULES, LANGUAGES  # rules file is the shared source

__all__ = ["LANGUAGES", "spell_number", "to_spoken"]

_SPOKEN: Final[dict[str, Any]] = _RULES["spoken"]

#: A written number: integer or decimal, either decimal separator.
_NUMBER: Final = r"\d{1,9}(?:[.,]\d{1,6})?"

# Interior separator inside a written abbreviation: "мм рт. ст.", "ммоль/л".
_ABBREV_GAP: Final = r"[\s./]*"
_ABBREV_MAX_WORD: Final = 4

#: What sits between the two halves of a paired measurement — "140/90",
#: "12 × 8". The hyphen is deliberately NOT here: "10-14 днів" is a range,
#: read "від десяти до чотирнадцяти", and rendering it as "десять на
#: чотирнадцять" would be wrong Ukrainian even though it scores identically.
_PAIR_SEP: Final = r"\s*[/×*]\s*"


def _rules(section: str, language: str) -> Any:
    return _RULES[section].get(language, _RULES[section].get("en"))


# ── numeral spelling ───────────────────────────────────────────────────


def _plural(n: int, forms: dict[str, str], language: str) -> str:
    """one / few / many for ``n``.

    English has no paucal: everything but exactly one takes the plural.
    Ukrainian keeps the Slavic rule, teens included — 11 mg is "одинадцять
    міліграмІВ", not "…міліграм".
    """
    if language != "uk":
        return forms["one"] if n == 1 else forms["many"]
    if 11 <= n % 100 <= 14:
        return forms["many"]
    last = n % 10
    if last == 1:
        return forms["one"]
    if 2 <= last <= 4:
        return forms["few"]
    return forms["many"]


def _spell_below_thousand(value: int, gender: str, table: dict[str, Any]) -> list[str]:
    words: list[str] = []
    hundreds = table["hundreds"]
    if value >= 100 and hundreds:
        words.append(hundreds[value // 100])
        value %= 100
    if 10 <= value <= 19:
        words.append(table["teens"][value - 10])
        return [w for w in words if w]
    if value >= 20:
        words.append(table["tens"][value // 10])
        value %= 10
    if value:
        words.append(table["ones"][gender][value])
    return [w for w in words if w]


def spell_number(value: int, language: str, *, gender: str = "m") -> str:
    """``875`` → "вісімсот сімдесят п'ять". Falls back to digits above 10⁶.

    ``gender`` agrees the final unit word with the noun that follows —
    "одна крапля" against "один міліграм" — and is also what makes the
    decimal reading come out right, since "ціла" is feminine.
    """
    table = _SPOKEN.get(language)
    if table is None or value < 0 or value >= 1_000_000:
        return str(value)
    if value == 0:
        return str(table["zero"])

    words: list[str] = []
    if value >= 1000:
        count = value // 1000
        scale = table["thousands"]
        # "тисяча міліграмів", not "одна тисяча міліграмів" — Ukrainian drops
        # the numeral before a bare thousand; English does not.
        if not (count == 1 and language == "uk"):
            words += _spell_below_thousand(count, str(scale.get("gender", "m")), table)
        words.append(_plural(count, scale, language))
        value %= 1000
    if value or not words:
        words += _spell_below_thousand(value, gender, table)
    return " ".join(w for w in words if w)


def _spell_decimal(whole: str, frac: str, language: str) -> str:
    """"7,2" → "сім цілих дві десятих" / "seven point two"."""
    table = _SPOKEN.get(language)
    if table is None:
        return f"{whole}.{frac}"
    whole_value = int(whole)
    marker = _plural(whole_value, table["whole"], language)

    if language != "uk":
        # English reads the fraction digit by digit: 6.85 is "six point
        # eight five", never "six point eighty-five".
        digits = " ".join(spell_number(int(d), language) for d in frac)
        return f"{spell_number(whole_value, language)} {marker} {digits}"

    fractions = table["fraction"]
    denominator = fractions.get(len(frac))
    if denominator is None:
        digits = " ".join(spell_number(int(d), language, gender="f") for d in frac)
        return f"{spell_number(whole_value, language, gender='f')} {marker} {digits}"
    frac_value = int(frac)
    return (
        f"{spell_number(whole_value, language, gender='f')} {marker} "
        f"{spell_number(frac_value, language, gender='f')} "
        f"{_plural(frac_value, denominator, language)}"
    )


# ── unit rendering ─────────────────────────────────────────────────────


def _unit_words(canonical: str, language: str, *, count: str, rate: bool) -> str:
    """The unit as spoken after ``count``.

    ``count`` is the written number the unit attaches to, so a decimal takes
    the genitive form ("…дві десятих ВІДСОТКА") and an integer takes the
    plural its last digits ask for.
    """
    table = _SPOKEN.get(language, {}).get("units", {})
    spec = table.get(canonical)
    if spec is None:
        return canonical
    if "phrase" in spec:
        return str(spec["phrase"])
    if rate and "rate" in spec:
        return str(spec["rate"])
    if "." in count or "," in count:
        return str(spec.get("frac", spec["many"]))
    return _plural(int(count), spec, language)


def _unit_gender(canonical: str, language: str) -> str:
    spec = _SPOKEN.get(language, {}).get("units", {}).get(canonical)
    if not isinstance(spec, dict):
        return "m"
    return str(spec.get("gender", "m"))


# ── written-form recognition ───────────────────────────────────────────


def _unit_patterns(language: str) -> tuple[str, dict[str, str]]:
    """One alternation matching every written unit, longest source first.

    Built from the SAME tables the forward direction reads, so a unit the
    scorer canonicalises is a unit the linter can propose words for — the
    two directions cannot drift into disagreeing about the vocabulary.
    """
    sources: dict[str, str] = {}

    for words, canonical in _rules("phrases", language) or ():
        if all(len(w) <= _ABBREV_MAX_WORD for w in words):
            # "мм рт. ст." — an abbreviation, so eat the dots including the
            # trailing one; leaving it behind yields "…стовпа.," mid-sentence.
            pattern = (r"\.?" + _ABBREV_GAP).join(map(re.escape, words)) + r"\.?"
        else:
            pattern = _ABBREV_GAP.join(map(re.escape, words))
        sources[pattern] = canonical

    for written, canonical in (_rules("units", language) or {}).items():
        sources[re.escape(written) + r"\.?\b"] = canonical
    sources[re.escape("%")] = "pct"

    ordered = sorted(sources, key=lambda p: (-len(p), p))
    return "|".join(f"(?:{p})" for p in ordered), sources


def _canonical_for(match_text: str, language: str) -> str | None:
    """Which canonical unit a matched written form denotes."""
    pattern, sources = _unit_cache(language)
    for source, canonical in sorted(sources.items(), key=lambda kv: -len(kv[0])):
        if re.fullmatch(source, match_text, flags=re.IGNORECASE):
            return canonical
    return None


_UNIT_CACHE: dict[str, tuple[str, dict[str, str]]] = {}


def _unit_cache(language: str) -> tuple[str, dict[str, str]]:
    if language not in _UNIT_CACHE:
        _UNIT_CACHE[language] = _unit_patterns(language)
    return _UNIT_CACHE[language]


_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _master_regex(language: str) -> re.Pattern[str]:
    """Ordered alternation over the four written shapes Epic A names.

    Order is the whole design: the compound forms have to win before the
    bare-number rule gets a chance, or "140/90" becomes two unrelated
    numbers and the "на" that carries the reading is lost.
    """
    if language in _RE_CACHE:
        return _RE_CACHE[language]
    units, _ = _unit_cache(language)
    denominators = "|".join(
        re.escape(d)
        for d in sorted(_SPOKEN.get(language, {}).get("rate_phrases", {}), key=len, reverse=True)
    ) or r"(?!)"
    pattern = (
        # A digit welded to letters is an identifier, not a measurement:
        # "HbA1c" is the name of an assay and reading it "HbA один c" is
        # both wrong and, in a gold transcript, a fabricated error. The
        # lookbehind refuses a preceding DIGIT too, or "B12" would fail at
        # the "1" and then happily match the "2". The second lookbehind
        # covers the hyphenated case ("COVID-19") while leaving a numeric
        # range ("10-14 днів") alone, since its left side is a digit.
        r"(?<![^\W_])(?<![^\W\d_]-)"
        # 875/125 мг, 140/90 мм рт. ст. — a paired measurement.
        rf"(?:(?P<pa>{_NUMBER}){_PAIR_SEP}(?P<pb>{_NUMBER})(?:\s*(?P<punit>{units}))?"
        # 5 мг/добу, 7,2 %, 68 хв — a quantity, optionally per a denominator.
        rf"|(?P<qn>{_NUMBER})\s*(?P<qunit>{units})(?:\s*/\s*(?P<qden>{denominators})\b)?"
        # 68/хв — a rate with no unit of its own.
        rf"|(?P<rn>{_NUMBER})\s*/\s*(?P<rden>{denominators})\b"
        # A bare number, provided no letter is glued to its tail.
        rf"|(?P<n>{_NUMBER})(?![^\W\d_]))"
    )
    compiled = re.compile(pattern, flags=re.IGNORECASE)
    _RE_CACHE[language] = compiled
    return compiled


def _spell_written(number: str, language: str, *, gender: str = "m") -> str:
    whole, _, frac = number.replace(",", ".").partition(".")
    if frac:
        return _spell_decimal(whole, frac, language)
    return spell_number(int(whole), language, gender=gender)


def to_spoken(text: str, language: str) -> str:
    """Rewrite every written numeral and unit in ``text`` as spoken words.

    Everything the tables do not recognise is returned untouched — the
    linter's job is to propose an improvement, never to mangle a sentence it
    only partly understood.
    """
    if language not in LANGUAGES or not text:
        return text
    spoken = _SPOKEN.get(language)
    if spoken is None:
        return text
    connector = str(spoken["connector"])
    rate_phrases = spoken.get("rate_phrases", {})

    def replace(match: re.Match[str]) -> str:
        groups = match.groupdict()

        if groups.get("pa") is not None:
            unit_text = groups.get("punit")
            canonical = _canonical_for(unit_text, language) if unit_text else None
            gender = _unit_gender(canonical, language) if canonical else "m"
            left = _spell_written(groups["pa"], language, gender=gender)
            right = _spell_written(groups["pb"], language, gender=gender)
            out = f"{left} {connector} {right}"
            if canonical:
                out += " " + _unit_words(
                    canonical, language, count=groups["pb"], rate=False
                )
            return out

        if groups.get("qn") is not None:
            canonical = _canonical_for(groups["qunit"], language)
            if canonical is None:
                return match.group(0)
            count = groups["qn"]
            words = _spell_written(
                count, language, gender=_unit_gender(canonical, language)
            )
            out = f"{words} {_unit_words(canonical, language, count=count, rate=False)}"
            denominator = groups.get("qden")
            if denominator:
                out += " " + str(rate_phrases[denominator.lower()])
            return out

        if groups.get("rn") is not None:
            words = _spell_written(groups["rn"], language)
            return f"{words} {rate_phrases[groups['rden'].lower()]}"

        return _spell_written(groups["n"], language)

    return _master_regex(language).sub(replace, text)
