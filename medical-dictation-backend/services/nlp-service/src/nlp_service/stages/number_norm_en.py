"""English number normalization.

Same strategy as the UK module — tag + pattern-match. The English
vocabulary is smaller (fewer declensions) but the pattern set is the
same: BP, HR, doses, frequencies, time, ranges, decimals.
"""

from __future__ import annotations

import re
from typing import Final

_UNITS: Final[dict[str, str]] = {
    "mg": "mg",
    "milligram": "mg",
    "milligrams": "mg",
    "ml": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "cm": "cm",
    "centimeter": "cm",
    "centimeters": "cm",
    "mm": "mm",
    "m": "m",
    "meter": "m",
    "meters": "m",
    "kg": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "g": "g",
    "gram": "g",
    "grams": "g",
    "l": "l",
    "liter": "l",
    "liters": "l",
    "ug": "ug",
    "mcg": "mcg",
    "microgram": "mcg",
    "micrograms": "mcg",
    "iu": "IU",
    "bpm": "bpm",
}

_BP_UNIT_SEQ = ("millimeters", "of", "mercury")  # → "mmHg"
_MMHG = {"mmhg", "mm", "hg"}

_DIGITS_EN: Final[dict[str, int]] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "thousand": 1000,
}

_HOUR_SPELLED = {
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
}


def _tokenize(text: str) -> list[str]:
    spaced = re.sub(r"([.,;:!?])", r" \1 ", text)
    return [t for t in spaced.split() if t]


def _digit_value(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _DIGITS_EN.get(token.lower())


def _parse_number_run(tokens: list[str], i: int) -> tuple[int | None, int]:
    """Greedy multi-word cardinal parser.

    Handles "one hundred twenty" = 120 and "two thousand five hundred" = 2500.
    Pure-digit tokens are returned as-is.
    """
    if i >= len(tokens):
        return None, 0
    first = _digit_value(tokens[i])
    if first is None:
        return None, 0
    if tokens[i].isdigit():
        return first, 1

    total = 0
    current = first
    consumed = 1
    cursor = i + 1

    # Colloquial BP-style parse: "one twenty" → 120, "two ten" → 210.
    # Rule: if the head is a SINGLE DIGIT (1–9) and the next token is
    # a TEENS or TENS value (10–90, no "hundred"), interpret as
    # ``head * 100 + next``. Disambiguation: only triggers when no
    # "hundred"/"thousand" follows (the more explicit form wins).
    if (
        1 <= current <= 9
        and cursor < len(tokens)
        and _digit_value(tokens[cursor]) is not None
        and tokens[cursor].lower() not in {"hundred", "thousand"}
    ):
        nxt = _digit_value(tokens[cursor])
        if nxt is not None and 10 <= nxt <= 99:
            # Peek ahead — if "hundred"/"thousand" follows, fall through
            # to the standard parser.
            two_ahead = tokens[cursor + 1].lower() if cursor + 1 < len(tokens) else ""
            if two_ahead not in {"hundred", "thousand"}:
                current = current * 100 + nxt
                cursor += 1
                consumed += 1
                # Continue walking for trailing units like "twenty five" → 25 stays.
                while cursor < len(tokens):
                    v = _digit_value(tokens[cursor])
                    if v is None or v >= 10:
                        break
                    current += v
                    cursor += 1
                    consumed += 1
                return current, consumed

    # Walk while consecutive tokens map to digits.
    while cursor < len(tokens):
        v = _digit_value(tokens[cursor])
        if v is None:
            break
        if v == 100:
            current = max(current, 1) * 100
        elif v == 1000:
            current = max(current, 1) * 1000
            total += current
            current = 0
        elif v < 100:
            current += v
        else:
            break
        cursor += 1
        consumed += 1
    return total + current, consumed


def normalize_en(text: str, *, decimal_separator: str, bp_separator: str) -> str:
    raw = _tokenize(text)
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        # ── BP-like: NUM over NUM (mmHg?) ───────────────────────────
        v1, c1 = _parse_number_run(raw, i)
        if v1 is not None and i + c1 < n and raw[i + c1].lower() in {"over", "/"}:
            v2, c2 = _parse_number_run(raw, i + c1 + 1)
            if v2 is not None:
                consumed = c1 + 1 + c2
                # Optional trailing "millimeters of mercury" / "mm hg"
                if (
                    i + consumed + len(_BP_UNIT_SEQ) <= n
                    and tuple(
                        t.lower() for t in raw[i + consumed : i + consumed + len(_BP_UNIT_SEQ)]
                    )
                    == _BP_UNIT_SEQ
                ):
                    out.append(f"{v1}{bp_separator}{v2} mmHg")
                    i += consumed + len(_BP_UNIT_SEQ)
                    continue
                if (
                    i + consumed + 1 < n
                    and raw[i + consumed].lower() in _MMHG
                    and raw[i + consumed + 1].lower() in _MMHG
                ):
                    out.append(f"{v1}{bp_separator}{v2} mmHg")
                    i += consumed + 2
                    continue
                out.append(f"{v1}{bp_separator}{v2}")
                i += consumed
                continue

        # ── Decimal: NUM point NUM ─────────────────────────────────
        if v1 is not None and i + c1 < n and raw[i + c1].lower() == "point":
            v2, c2 = _parse_number_run(raw, i + c1 + 1)
            if v2 is not None:
                out.append(f"{v1}{decimal_separator}{v2}")
                i += c1 + 1 + c2
                continue

        # ── Range: from NUM to NUM ─────────────────────────────────
        if raw[i].lower() == "from" and i + 1 < n:
            va, ca = _parse_number_run(raw, i + 1)
            if va is not None and i + 1 + ca < n and raw[i + 1 + ca].lower() == "to":
                vb, cb = _parse_number_run(raw, i + 2 + ca)
                if vb is not None:
                    out.append(f"{va}–{vb}")
                    i += 2 + ca + cb
                    continue

        # ── Half past NUM ──────────────────────────────────────────
        if i + 2 < n and raw[i].lower() == "half" and raw[i + 1].lower() == "past":
            hour = _HOUR_SPELLED.get(raw[i + 2].lower())
            if hour is not None:
                out.append(f"{hour:02d}:30")
                i += 3
                continue

        # ── NUM times a day ────────────────────────────────────────
        if (
            v1 is not None
            and i + c1 + 2 < n
            and raw[i + c1].lower() in {"times", "time"}
            and raw[i + c1 + 1].lower() in {"a", "per"}
            and raw[i + c1 + 2].lower() in {"day", "daily"}
        ):
            out.append(f"{v1}x/day")
            i += c1 + 3
            continue

        # ── Generic: NUM UNIT ──────────────────────────────────────
        if v1 is not None and i + c1 < n:
            unit_word = raw[i + c1].lower()
            if unit_word in _UNITS:
                out.append(f"{v1} {_UNITS[unit_word]}")
                i += c1 + 1
                continue

        # ── Multi-word spelled cardinal → digit ────────────────────
        if v1 is not None and c1 > 1:
            out.append(str(v1))
            i += c1
            continue

        out.append(raw[i])
        i += 1

    return _detokenize(out)


def _detokenize(tokens: list[str]) -> str:
    out: list[str] = []
    for tok in tokens:
        if out and tok in {".", ",", ";", ":", "!", "?"}:
            out[-1] = out[-1] + tok
        else:
            out.append(tok)
    return " ".join(out)
