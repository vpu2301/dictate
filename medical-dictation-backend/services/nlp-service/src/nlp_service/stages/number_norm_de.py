"""German number normalization.

Same contract as the UK/EN modules — tag + pattern-match, and pass
through unchanged whenever the reading is doubtful (ADR-0015). Two
things make German structurally different from the other two:

1. **Numerals are single compound tokens.** "einhundertvierzig",
   "zweiundzwanzig", "dreitausendfünfhundert" arrive from Whisper as ONE
   word, so the parser is word-internal (``_parse_compound``) rather
   than a multi-token run. The unit-position order is inverted too:
   "vierundzwanzig" is *four-and-twenty*.
2. **Folding a bare numeral is riskier, not safer.** As in EN, a
   standalone spelled numeral passes through as words; it is only
   rewritten as digits when a unit follows or the numeral is a compound
   (contains und/hundert/tausend). "Der Patient kam um acht" keeps its
   "acht" — that is prose, not a measurement.

Blood pressure is the one place where an aggressive reading is patient
harm, so the slash form is only emitted with an explicit mmHg unit, a
BP cue word in front, or two physiologically plausible values — exactly
the gate the UK/EN modules use.
"""

from __future__ import annotations

import re
from typing import Final

# Canonical German unit vocabulary. Keys are what a clinician says (or
# what Whisper writes); values are what lands in the note.
_UNITS: Final[dict[str, str]] = {
    "mg": "mg",
    "milligramm": "mg",
    "ml": "ml",
    "milliliter": "ml",
    "cm": "cm",
    "zentimeter": "cm",
    "mm": "mm",
    "millimeter": "mm",
    "m": "m",
    "meter": "m",
    "kg": "kg",
    "kilogramm": "kg",
    "kilo": "kg",
    "g": "g",
    "gramm": "g",
    "l": "l",
    "liter": "l",
    "µg": "µg",
    "mikrogramm": "µg",
    "mcg": "µg",
    "mmol": "mmol",
    "millimol": "mmol",
    "ie": "IE",
    "einheiten": "IE",
    "prozent": "%",
    "%": "%",
}

# Multi-word units, longest first. Matched as a token sequence after a
# number ("120 Millimeter Quecksilbersäule" → "120 mmHg").
_UNIT_SEQUENCES: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("millimeter", "quecksilbersäule"), "mmHg"),
    (("millimeter", "quecksilber"), "mmHg"),
    (("internationale", "einheiten"), "IE"),
    (("grad", "celsius"), "°C"),
)

# Abbreviated BP unit, spoken letter-by-letter ("mm Hg").
_MMHG: Final = {"mmhg", "mm", "hg"}

_DIGIT_WORDS: Final[dict[str, int]] = {
    "null": 0,
    "ein": 1,
    "eine": 1,
    "eins": 1,
    "zwei": 2,
    "zwo": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
}
_TEENS: Final[dict[str, int]] = {
    "zehn": 10,
    "elf": 11,
    "zwölf": 12,
    "dreizehn": 13,
    "vierzehn": 14,
    "fünfzehn": 15,
    "sechzehn": 16,
    "siebzehn": 17,
    "achtzehn": 18,
    "neunzehn": 19,
}
_TENS: Final[dict[str, int]] = {
    "zwanzig": 20,
    "dreissig": 30,
    "vierzig": 40,
    "fünfzig": 50,
    "sechzig": 60,
    "siebzig": 70,
    "achtzig": 80,
    "neunzig": 90,
}

_SEP_BP: Final = {"zu", "auf", "/"}
_BP_CUES_DE: Final[frozenset[str]] = frozenset(
    {"blutdruck", "rr", "bd", "systolisch", "diastolisch", "druck"}
)
_DECIMAL_WORDS: Final = {"komma"}
_BP_SYSTOLIC = range(60, 301)
_BP_DIASTOLIC = range(30, 161)


def _fold(word: str) -> str:
    """Normalize a German token for lookup: case, ß, hyphens."""
    return word.lower().replace("ß", "ss").replace("-", "")


def _parse_below_hundred(w: str) -> int | None:
    if not w:
        return 0
    if w in _TEENS:
        return _TEENS[w]
    if w in _TENS:
        return _TENS[w]
    if w in _DIGIT_WORDS:
        return _DIGIT_WORDS[w]
    # "einundzwanzig" — unit UND tens, in that order.
    head, sep, tail = w.partition("und")
    if sep and head in _DIGIT_WORDS and tail in _TENS:
        unit = _DIGIT_WORDS[head]
        if unit == 0:
            return None
        return unit + _TENS[tail]
    return None


def _parse_below_thousand(w: str) -> int | None:
    if not w:
        return 0
    total = 0
    head, sep, tail = w.partition("hundert")
    if sep:
        if head == "":
            hundreds = 1
        else:
            hundreds = _DIGIT_WORDS.get(head, 0)
            if not 1 <= hundreds <= 9:
                return None
        total += hundreds * 100
        w = tail
        if not w:
            return total
    rest = _parse_below_hundred(w)
    if rest is None:
        return None
    return total + rest


def _parse_compound(word: str) -> int | None:
    """Value of a single spelled German numeral token, or None."""
    w = _fold(word)
    if not w:
        return None
    total = 0
    head, sep, tail = w.partition("tausend")
    if sep:
        if head == "":
            thousands = 1
        else:
            thousands = _parse_below_thousand(head) or 0
            if thousands < 1:
                return None
        total += thousands * 1000
        w = tail
        if not w:
            return total
    rest = _parse_below_thousand(w)
    if rest is None:
        return None
    return total + rest


def _is_compound(word: str) -> bool:
    """True when the numeral carries its own structure ("zweiundzwanzig",
    "einhundertvierzig"). A bare "acht" is not — folding it would rewrite
    prose."""
    w = _fold(word)
    return ("und" in w or "hundert" in w or "tausend" in w) and not w.isdigit()


def _tokenize(text: str) -> list[str]:
    spaced = re.sub(r"([.,;:!?])", r" \1 ", text)
    return [t for t in spaced.split() if t]


def _digit_value(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _parse_compound(token)


def _parse_number(tokens: list[str], i: int) -> tuple[int | None, int, bool]:
    """Return ``(value, tokens_consumed, foldable)``.

    ``foldable`` says whether the value may be WRITTEN as digits on its
    own: digits already are, and a compound numeral is unambiguous. A
    bare spelled numeral is not — see the module docstring.
    """
    if i >= len(tokens):
        return None, 0, False
    tok = tokens[i]
    if tok.isdigit():
        return int(tok), 1, True
    value = _parse_compound(tok)
    if value is None:
        return None, 0, False
    return value, 1, _is_compound(tok)


def _parse_fraction_digits(tokens: list[str], i: int) -> tuple[str | None, int]:
    """Spoken decimal tail as a literal digit string.

    "null fünf" → "05". Summing would collapse it to 5 and silently turn
    5,05 into 5,5 — a dropped digit in a dose is patient harm.
    """
    digits: list[str] = []
    cursor = i
    while cursor < len(tokens):
        tok = tokens[cursor]
        if tok.isdigit():
            digits.append(tok)
            cursor += 1
            continue
        v = _DIGIT_WORDS.get(_fold(tok))
        if v is None:
            break
        digits.append(str(v))
        cursor += 1
    if not digits:
        return None, 0
    return "".join(digits), cursor - i


def _looks_like_bp(v1: int, v2: int) -> bool:
    return v1 in _BP_SYSTOLIC and v2 in _BP_DIASTOLIC


def _has_bp_cue_de(tokens: list[str], i: int) -> bool:
    return any(_fold(t) in _BP_CUES_DE for t in tokens[max(0, i - 3) : i])


def _match_unit_sequence(tokens: list[str], i: int) -> tuple[str | None, int]:
    for seq, canonical in _UNIT_SEQUENCES:
        if i + len(seq) <= len(tokens) and tuple(_fold(t) for t in tokens[i : i + len(seq)]) == seq:
            return canonical, len(seq)
    return None, 0


def normalize_de(text: str, *, decimal_separator: str, bp_separator: str) -> str:
    raw = _tokenize(text)
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        v1, c1, foldable = _parse_number(raw, i)

        # ── BP-like: NUM zu NUM (mmHg?) ─────────────────────────────
        if v1 is not None and i + c1 < n and _fold(raw[i + c1]) in _SEP_BP:
            v2, c2, _ = _parse_number(raw, i + c1 + 1)
            if v2 is not None:
                consumed = c1 + 1 + c2
                unit, ulen = _match_unit_sequence(raw, i + consumed)
                if unit == "mmHg":
                    out.append(f"{v1}{bp_separator}{v2} mmHg")
                    i += consumed + ulen
                    continue
                if (
                    i + consumed + 1 < n
                    and _fold(raw[i + consumed]) in _MMHG
                    and _fold(raw[i + consumed + 1]) in _MMHG
                ):
                    out.append(f"{v1}{bp_separator}{v2} mmHg")
                    i += consumed + 2
                    continue
                if i + consumed < n and _fold(raw[i + consumed]) == "mmhg":
                    out.append(f"{v1}{bp_separator}{v2} mmHg")
                    i += consumed + 1
                    continue
                # No unit: the slash form needs a cue or plausible values,
                # else "drei zu vier" would be mangled into "3/4".
                if _has_bp_cue_de(raw, i) or _looks_like_bp(v1, v2):
                    out.append(f"{v1}{bp_separator}{v2}")
                    i += consumed
                    continue

        # ── Decimal: NUM Komma NUM ──────────────────────────────────
        if v1 is not None and i + c1 < n and _fold(raw[i + c1]) in _DECIMAL_WORDS:
            frac, cf = _parse_fraction_digits(raw, i + c1 + 1)
            if frac is not None:
                whole = f"{v1}{decimal_separator}{frac}"
                unit, ulen = _match_unit_sequence(raw, i + c1 + 1 + cf)
                if unit is not None:
                    out.append(f"{whole} {unit}")
                    i += c1 + 1 + cf + ulen
                    continue
                nxt = i + c1 + 1 + cf
                if nxt < n and _fold(raw[nxt]) in _UNITS:
                    out.append(f"{whole} {_UNITS[_fold(raw[nxt])]}")
                    i += c1 + 2 + cf
                    continue
                out.append(whole)
                i += c1 + 1 + cf
                continue

        # ── Range: von NUM bis NUM ──────────────────────────────────
        if _fold(raw[i]) == "von" and i + 1 < n:
            va, ca, _ = _parse_number(raw, i + 1)
            if va is not None and i + 1 + ca < n and _fold(raw[i + 1 + ca]) == "bis":
                vb, cb, _ = _parse_number(raw, i + 2 + ca)
                if vb is not None:
                    consumed = 2 + ca + cb
                    unit, ulen = _match_unit_sequence(raw, i + consumed)
                    if unit is None and i + consumed < n and _fold(raw[i + consumed]) in _UNITS:
                        unit, ulen = _UNITS[_fold(raw[i + consumed])], 1
                    if unit is not None:
                        out.append(f"{va}–{vb} {unit}")
                        i += consumed + ulen
                        continue
                    out.append(f"{va}–{vb}")
                    i += consumed
                    continue

        # ── Frequency: "dreimal täglich" / "3 mal pro Tag" → 3x/Tag ──
        freq = _match_frequency(raw, i)
        if freq is not None:
            rendered, consumed = freq
            out.append(rendered)
            i += consumed
            continue

        # ── Rate: "achtzig pro Minute" → "80/min" ───────────────────
        # The rate phrase is itself the disambiguator, so a bare spelled
        # numeral is safe to fold here (pulse and respiratory rate are
        # dictated exactly this way).
        if (
            v1 is not None
            and i + c1 + 1 < n
            and _fold(raw[i + c1]) in {"pro", "je"}
            and _fold(raw[i + c1 + 1]) == "minute"
        ):
            out.append(f"{v1}/min")
            i += c1 + 2
            continue

        # ── Generic: NUM UNIT ───────────────────────────────────────
        if v1 is not None:
            unit, ulen = _match_unit_sequence(raw, i + c1)
            if unit is not None:
                out.append(f"{v1} {unit}")
                i += c1 + ulen
                continue
            if i + c1 < n and _fold(raw[i + c1]) in _UNITS:
                out.append(f"{v1} {_UNITS[_fold(raw[i + c1])]}")
                i += c1 + 1
                continue

        # ── Compound numeral standing alone → digits ────────────────
        if v1 is not None and foldable and not raw[i].isdigit():
            out.append(str(v1))
            i += c1
            continue

        out.append(raw[i])
        i += 1

    return _detokenize(out)


_FREQ_SUFFIX = re.compile(r"^(.+)mal$")
_FREQ_TAIL: Final = ({"täglich"}, {"pro", "tag"}, {"am", "tag"})


def _match_frequency(tokens: list[str], i: int) -> tuple[str, int] | None:
    """ "dreimal täglich" / "3 mal pro Tag" → "3x/Tag"."""
    n = len(tokens)
    value: int | None = None
    cursor = i
    m = _FREQ_SUFFIX.match(_fold(tokens[i]))
    if m:
        value = _digit_value(m.group(1))
        cursor = i + 1
    else:
        v, c, _ = _parse_number(tokens, i)
        if v is not None and i + c < n and _fold(tokens[i + c]) == "mal":
            value = v
            cursor = i + c + 1
    if value is None or cursor >= n:
        return None
    if _fold(tokens[cursor]) == "täglich":
        return f"{value}x/Tag", cursor + 1 - i
    if (
        cursor + 1 < n
        and _fold(tokens[cursor]) in {"pro", "am", "je"}
        and _fold(tokens[cursor + 1]) == "tag"
    ):
        return f"{value}x/Tag", cursor + 2 - i
    return None


def _detokenize(tokens: list[str]) -> str:
    out: list[str] = []
    for tok in tokens:
        if out and tok in {".", ",", ";", ":", "!", "?"}:
            out[-1] = out[-1] + tok
        else:
            out.append(tok)
    return " ".join(out)
