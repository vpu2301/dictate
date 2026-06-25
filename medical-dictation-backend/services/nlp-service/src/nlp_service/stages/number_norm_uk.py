"""Ukrainian number normalization.

Strategy:
1. Tokenize on whitespace (preserving punctuation as separate tokens
   where reasonable).
2. Tag each token as NUM / UNIT / SEP / OTHER.
3. Walk the stream applying pattern rules in PRIORITY order:
   - BP-like ``NUM на NUM`` (with or without trailing unit)
   - HR ``пульс NUM`` (with various trailing forms)
   - ``NUM раз(ів) на (добу|день)``
   - decimal ``NUM цілих NUM``
   - half-time ``пів на NUM``
   - range ``від NUM до NUM``
   - generic ``NUM UNIT``
4. Untagged numbers (no surrounding semantic markers) pass through.

The output preserves all non-recognized tokens verbatim. Determinism:
no random fallback, no float ops, ordered iteration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_UNITS: Final[dict[str, str]] = {
    "мг": "мг",
    "міліграм": "мг",
    "міліграми": "мг",
    "міліграмів": "мг",
    "мл": "мл",
    "мілілітр": "мл",
    "мілілітри": "мл",
    "мілілітрів": "мл",
    "см": "см",
    "сантиметр": "см",
    "сантиметри": "см",
    "сантиметрів": "см",
    "мм": "мм",
    "м": "м",
    "метр": "м",
    "метрів": "м",
    "кг": "кг",
    "кілограм": "кг",
    "кілограми": "кг",
    "кілограмів": "кг",
    "г": "г",
    "грам": "г",
    "грами": "г",
    "грамів": "г",
    "л": "л",
    "літр": "л",
    "літрів": "л",
    "мкг": "мкг",
    "мікрограм": "мкг",
    "мікрограмів": "мкг",
}

_BP_UNIT_SEQ = ("міліметрів", "ртутного", "стовпчика")  # → "мм рт. ст."

_SEP_NA = {"на"}
_HALF = {"пів"}
_DET = {"приблизно", "близько"}
_OF_DAY = {"добу", "день"}

_RAZ = {"раз", "рази", "разів", "разу"}
_CILIH = {"цілих", "цілі", "ціла"}
_FROM = {"від"}
_TO = {"до"}

# Spelled-out cardinals → digit string. We accept compositions like
# "сто двадцять" → "120" via a recursive parser.
_DIGITS_UK: Final[dict[str, int]] = {
    "нуль": 0,
    "один": 1,
    "одна": 1,
    "одну": 1,
    "одного": 1,
    "одної": 1,
    "два": 2,
    "дві": 2,
    "двох": 2,
    "три": 3,
    "трьох": 3,
    "чотири": 4,
    "чотирьох": 4,
    "п'ять": 5,
    "п'яти": 5,
    "пять": 5,
    "пяти": 5,
    "шість": 6,
    "шести": 6,
    "сім": 7,
    "семи": 7,
    "вісім": 8,
    "восьми": 8,
    "дев'ять": 9,
    "дев'яти": 9,
    "девять": 9,
    "девяти": 9,
    "десять": 10,
    "десяти": 10,
    "одинадцять": 11,
    "одинадцяти": 11,
    "дванадцять": 12,
    "дванадцяти": 12,
    "тринадцять": 13,
    "тринадцяти": 13,
    "чотирнадцять": 14,
    "чотирнадцяти": 14,
    "п'ятнадцять": 15,
    "п'ятнадцяти": 15,
    "шістнадцять": 16,
    "шістнадцяти": 16,
    "сімнадцять": 17,
    "сімнадцяти": 17,
    "вісімнадцять": 18,
    "вісімнадцяти": 18,
    "дев'ятнадцять": 19,
    "дев'ятнадцяти": 19,
    "двадцять": 20,
    "двадцяти": 20,
    "тридцять": 30,
    "тридцяти": 30,
    "сорок": 40,
    "сорока": 40,
    "п'ятдесят": 50,
    "п'ятдесяти": 50,
    "пятдесят": 50,
    "пятдесяти": 50,
    "шістдесят": 60,
    "шістдесяти": 60,
    "сімдесят": 70,
    "сімдесяти": 70,
    "вісімдесят": 80,
    "вісімдесяти": 80,
    "дев'яносто": 90,
    "дев'яноста": 90,
    "девяносто": 90,
    "девяноста": 90,
    "сто": 100,
    "ста": 100,
    "двісті": 200,
    "двохсот": 200,
    "триста": 300,
    "трьохсот": 300,
    "чотириста": 400,
    "чотирьохсот": 400,
    "п'ятсот": 500,
    "п'ятисот": 500,
    "шістсот": 600,
    "шестисот": 600,
    "сімсот": 700,
    "семисот": 700,
    "вісімсот": 800,
    "восьмисот": 800,
    "дев'ятсот": 900,
    "дев'ятисот": 900,
    "тисяча": 1000,
    "тисячі": 1000,
    "тисяч": 1000,
}

_HOUR_SPELLED = {
    "восьму": 8,
    "сьому": 7,
    "шосту": 6,
    "дев'яту": 9,
    "десяту": 10,
    "одинадцяту": 11,
    "дванадцяту": 12,
    "першу": 1,
    "другу": 2,
    "третю": 3,
    "четверту": 4,
    "п'яту": 5,
}


class Tag(StrEnum):
    NUM = "NUM"
    UNIT = "UNIT"
    SEP_NA = "SEP_NA"
    OTHER = "OTHER"


@dataclass(slots=True)
class _Tok:
    text: str
    tag: Tag
    value: int | None = None  # only for NUM


def _tokenize(text: str) -> list[str]:
    """Whitespace + punctuation-aware tokenisation.

    Keeps trailing punctuation on its own token so pattern matching sees
    clean word boundaries.
    """
    # Pull punctuation off word boundaries.
    spaced = re.sub(r"([.,;:!?])", r" \1 ", text)
    return [t for t in spaced.split() if t]


def _digit_value(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _DIGITS_UK.get(token.lower())


def _parse_number_run(tokens: list[str], i: int) -> tuple[int | None, int]:
    """Greedy multi-word cardinal parser.

    Returns ``(value, words_consumed)``. ``value`` is None if the
    current position isn't a number.

    Handles: 'сто двадцять' = 120; 'тисяча двісті' = 1200; pure-digit
    strings ('120') pass through.
    """
    if i >= len(tokens):
        return None, 0
    first = _digit_value(tokens[i])
    if first is None:
        return None, 0
    if tokens[i].isdigit():
        return first, 1

    total = first
    consumed = 1
    cursor = i + 1
    # Append additional summable terms while consecutive UK words map
    # to smaller-magnitude digits.
    while cursor < len(tokens):
        v = _digit_value(tokens[cursor])
        if v is None:
            break
        # Allow only descending magnitudes after the head.
        if v >= total and total < 100:
            break
        total += v
        cursor += 1
        consumed += 1
    return total, consumed


def _parse_fraction_digits(tokens: list[str], i: int) -> tuple[str | None, int]:
    """Parse a spoken decimal fraction as a literal digit string.

    "нуль п'ять" → "05", "п'ять" → "5". The summing run-parser collapses
    "нуль п'ять" to 0 and drops the trailing digit (2.05 → 2.0) — a dropped
    digit in a dose decimal is patient harm — so the fractional part is
    rendered digit-by-digit, preserving leading zeros.
    """
    digits: list[str] = []
    cursor = i
    while cursor < len(tokens):
        v = _digit_value(tokens[cursor])
        if v is None:
            break
        digits.append(str(v))
        cursor += 1
    if not digits:
        return None, 0
    return "".join(digits), cursor - i


# Plausible BP ranges — gate the "NUM на NUM" → slash rewrite so the common
# preposition "на" (dimensions, "for N days") is not turned into a slash.
_BP_SYSTOLIC = range(60, 301)
_BP_DIASTOLIC = range(30, 161)
_BP_CUES_UK: Final[frozenset[str]] = frozenset(
    {"тиск", "ат", "артеріальний", "артеріального", "артеріальним"}
)


def _looks_like_bp(v1: int, v2: int) -> bool:
    return v1 in _BP_SYSTOLIC and v2 in _BP_DIASTOLIC


def _has_bp_cue_uk(tokens: list[str], i: int) -> bool:
    return any(t.lower() in _BP_CUES_UK for t in tokens[max(0, i - 3) : i])


# ── Patterns ────────────────────────────────────────────────────────


def normalize_uk(text: str, *, decimal_separator: str, bp_separator: str) -> str:
    raw = _tokenize(text)
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        # ── BP-like: NUM на NUM (UNIT?) ────────────────────────────
        v1, c1 = _parse_number_run(raw, i)
        if v1 is not None and i + c1 < n and raw[i + c1].lower() in _SEP_NA:
            v2, c2 = _parse_number_run(raw, i + c1 + 1)
            if v2 is not None:
                consumed = c1 + 1 + c2
                # Optional trailing BP unit phrase: "міліметрів ртутного
                # стовпчика" — an explicit unit is the strongest BP signal.
                if (
                    i + consumed + len(_BP_UNIT_SEQ) <= n
                    and tuple(
                        t.lower() for t in raw[i + consumed : i + consumed + len(_BP_UNIT_SEQ)]
                    )
                    == _BP_UNIT_SEQ
                ):
                    out.append(f"{v1}{bp_separator}{v2} мм рт. ст.")
                    i += consumed + len(_BP_UNIT_SEQ)
                    continue
                # No unit: emit the slash only when a BP cue precedes or both
                # values are physiologically plausible — otherwise "три на
                # чотири" must pass through unchanged (ADR-0015).
                if _has_bp_cue_uk(raw, i) or _looks_like_bp(v1, v2):
                    out.append(f"{v1}{bp_separator}{v2}")
                    i += consumed
                    continue

        # ── Decimal: NUM цілих NUM ─────────────────────────────────
        if v1 is not None and i + c1 < n and raw[i + c1].lower() in _CILIH:
            frac, cf = _parse_fraction_digits(raw, i + c1 + 1)
            if frac is not None:
                out.append(f"{v1}{decimal_separator}{frac}")
                i += c1 + 1 + cf
                continue

        # ── Range: від NUM до NUM ──────────────────────────────────
        if raw[i].lower() in _FROM and i + 1 < n:
            va, ca = _parse_number_run(raw, i + 1)
            if va is not None and i + 1 + ca < n and raw[i + 1 + ca].lower() in _TO:
                vb, cb = _parse_number_run(raw, i + 2 + ca)
                if vb is not None:
                    out.append(f"{va}–{vb}")
                    i += 2 + ca + cb
                    continue

        # ── Half-time: [о] пів на NUM_SPELLED_HOUR ─────────────────
        # Consume optional preposition "о" so "о пів на восьму" → "07:30".
        half_start = i
        if raw[i].lower() == "о" and i + 1 < n and raw[i + 1].lower() in _HALF:
            half_start = i + 1
        if (
            half_start < n
            and raw[half_start].lower() in _HALF
            and half_start + 2 < n
            and raw[half_start + 1].lower() in _SEP_NA
        ):
            hour = _HOUR_SPELLED.get(raw[half_start + 2].lower())
            if hour is not None:
                out.append(f"{hour - 1:02d}:30")
                i = half_start + 3
                continue

        # ── HR: пульс NUM (ударів)? (за хвилину)? → пульс NUM/хв ──
        if raw[i].lower() == "пульс" and i + 1 < n:
            vh, ch = _parse_number_run(raw, i + 1)
            if vh is not None:
                tail = i + 1 + ch
                # Optional "ударів"
                if tail < n and raw[tail].lower() in {"ударів", "удари", "удар"}:
                    tail += 1
                # Optional "за хвилину"
                if (
                    tail + 1 < n
                    and raw[tail].lower() == "за"
                    and raw[tail + 1].lower() in {"хвилину", "хвилин"}
                ):
                    tail += 2
                    out.append(f"пульс {vh}/хв")
                    i = tail
                    continue
                # Without "за хвилину" / "ударів", treat as plain "пульс NUM"
                if tail > i + 1 + ch:
                    out.append(f"пульс {vh}/хв")
                    i = tail
                    continue

        # ── NUM раз(ів) на (добу|день) ─────────────────────────────
        if (
            v1 is not None
            and i + c1 + 2 < n
            and raw[i + c1].lower() in _RAZ
            and raw[i + c1 + 1].lower() == "на"
            and raw[i + c1 + 2].lower() in _OF_DAY
        ):
            # Canonical frequency form: "X разів/добу" | "X разів/день".
            out.append(
                f"{v1} разів/добу" if raw[i + c1 + 2].lower() == "добу" else f"{v1} разів/день"
            )
            i += c1 + 3
            continue

        # ── Generic: NUM UNIT ──────────────────────────────────────
        if v1 is not None and i + c1 < n:
            unit_word = raw[i + c1].lower()
            if unit_word in _UNITS:
                out.append(f"{v1} {_UNITS[unit_word]}")
                i += c1 + 1
                continue

        # ── Pure digit / spelled cardinal with no surrounding markers ──
        if v1 is not None and c1 > 1:
            # Multi-word spelled cardinal with no unit nearby → fold to digit
            # ONLY if it's "long enough" to be unambiguous (avoids touching
            # 'один' as a determiner).
            out.append(str(v1))
            i += c1
            continue

        out.append(raw[i])
        i += 1

    # Re-glue with smart spacing around punctuation we extracted.
    return _detokenize(out)


def _detokenize(tokens: list[str]) -> str:
    out: list[str] = []
    for tok in tokens:
        if out and tok in {".", ",", ";", ":", "!", "?"}:
            out[-1] = out[-1] + tok
        else:
            out.append(tok)
    return " ".join(out)
