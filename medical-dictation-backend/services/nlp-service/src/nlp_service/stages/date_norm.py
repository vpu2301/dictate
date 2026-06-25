"""Stage 4 — date & time normalization.

Two parsers operating on the post-Stage-3 text:

- **Relative**: "сьогодні"/"today", "вчора"/"yesterday", "завтра"/"tomorrow",
  "у п'ятницю"/"on Friday", "наступного тижня"/"next week", "минулого місяця"/"last month".
  Anchored to ``ctx.reference_date``.
- **Absolute**: "1 травня 2026", "May 1, 2026", "перше травня двадцять
  двадцять шостого" (spelled-out Ukrainian year).

Output respects ``ctx.date_format``:
- ``DD.MM.YYYY`` (default Ukrainian)
- ``YYYY-MM-DD`` (ISO)
- ``WORD`` (e.g., "1 травня 2026")

Ambiguous dates (e.g., "31.04.2026") are NOT corrected; they pass
through with a ``Warning{code="ambiguous_date"}`` for sprint-8 clinical
rules.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta

from ..pipeline.base import (
    PipelineWarning,
    ProcessingContext,
    StageInput,
    StageOutput,
)

logger = logging.getLogger(__name__)

# ── Vocab ───────────────────────────────────────────────────────────

_WEEKDAYS_UK = {
    "понеділок": 0,
    "понеділка": 0,
    "вівторок": 1,
    "вівторка": 1,
    "середа": 2,
    "середу": 2,
    "четвер": 3,
    "четверга": 3,
    "п'ятниця": 4,
    "п'ятницю": 4,
    "пятницю": 4,
    "пятниця": 4,
    "субота": 5,
    "суботу": 5,
    "неділя": 6,
    "неділю": 6,
}
_WEEKDAYS_EN = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_MONTHS_UK = {
    "січень": 1,
    "січня": 1,
    "лютий": 2,
    "лютого": 2,
    "березень": 3,
    "березня": 3,
    "квітень": 4,
    "квітня": 4,
    "травень": 5,
    "травня": 5,
    "червень": 6,
    "червня": 6,
    "липень": 7,
    "липня": 7,
    "серпень": 8,
    "серпня": 8,
    "вересень": 9,
    "вересня": 9,
    "жовтень": 10,
    "жовтня": 10,
    "листопад": 11,
    "листопада": 11,
    "грудень": 12,
    "грудня": 12,
}
_MONTH_NAMES_UK = {
    v: k
    for k, v in _MONTHS_UK.items()
    if k
    in {
        "січня",
        "лютого",
        "березня",
        "квітня",
        "травня",
        "червня",
        "липня",
        "серпня",
        "вересня",
        "жовтня",
        "листопада",
        "грудня",
    }
}
_MONTHS_EN = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MONTH_NAMES_EN = {v: k for k, v in _MONTHS_EN.items()}


# Spelled-out Ukrainian ordinal days in the genitive case, as clinicians
# dictate them: "третього травня" (the third of May). Number normalization
# (Stage 3) only knows cardinals ("три"), so these ordinals reach Stage 4
# as words and must be mapped here.
_ORD_UNITS_UK = {
    "першого": 1,
    "другого": 2,
    "третього": 3,
    "четвертого": 4,
    "п'ятого": 5,
    "пятого": 5,
    "шостого": 6,
    "сьомого": 7,
    "восьмого": 8,
    "дев'ятого": 9,
    "девятого": 9,
}
_ORD_TEENS_UK = {
    "десятого": 10,
    "одинадцятого": 11,
    "дванадцятого": 12,
    "тринадцятого": 13,
    "чотирнадцятого": 14,
    "п'ятнадцятого": 15,
    "пятнадцятого": 15,
    "шістнадцятого": 16,
    "сімнадцятого": 17,
    "вісімнадцятого": 18,
    "дев'ятнадцятого": 19,
    "девятнадцятого": 19,
}


def _build_ordinal_days_uk() -> dict[str, int]:
    out: dict[str, int] = {}
    out.update(_ORD_UNITS_UK)
    out.update(_ORD_TEENS_UK)
    out["двадцятого"] = 20
    out["тридцятого"] = 30
    for word, unit in _ORD_UNITS_UK.items():
        out[f"двадцять {word}"] = 20 + unit
    out["тридцять першого"] = 31
    return out


_ORD_DAYS_UK = _build_ordinal_days_uk()


class DateNormStage:
    """Sprint-05 Stage 4."""

    name = "date_norm"
    runs_on_partials: bool = False

    async def process(self, ctx: ProcessingContext, input: StageInput) -> StageOutput:
        t0 = time.monotonic()
        warnings = list(input.warnings)
        new_text = input.text

        new_text, w1 = _apply_relative(new_text, ctx)
        warnings.extend(w1)
        new_text, w2 = _apply_absolute(new_text, ctx)
        warnings.extend(w2)
        new_text, w3 = _apply_time(new_text, ctx)
        warnings.extend(w3)

        return StageOutput(
            text=new_text,
            words=input.words,
            confidence_spans=input.confidence_spans,
            voice_commands=input.voice_commands,
            operations=input.operations,
            warnings=tuple(warnings),
            metadata={
                self.name + ".latency_ms": (time.monotonic() - t0) * 1000.0,
                self.name + ".changed": new_text != input.text,
            },
        )


# ── Relative ────────────────────────────────────────────────────────


_REL_UK = {
    "сьогодні": 0,
    "вчора": -1,
    "учора": -1,
    "позавчора": -2,
    "завтра": 1,
    "післязавтра": 2,
}
_REL_EN = {
    "today": 0,
    "yesterday": -1,
    "tomorrow": 1,
}


def _apply_relative(text: str, ctx: ProcessingContext) -> tuple[str, list[PipelineWarning]]:
    warnings: list[PipelineWarning] = []
    table = _REL_UK if ctx.language == "uk" else _REL_EN

    def _replace_simple(m: re.Match[str]) -> str:
        word = m.group(0).lower()
        offset = table.get(word)
        if offset is None:
            return m.group(0)
        d = ctx.reference_date + timedelta(days=offset)
        return _format_date(d, ctx)

    pattern = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in table) + r")\b",
        re.IGNORECASE | re.UNICODE,
    )
    text = pattern.sub(_replace_simple, text)

    # "next/last week|month" + Ukrainian "наступного/минулого тижня/місяця"
    if ctx.language == "uk":
        text = re.sub(
            r"\bнаступного\s+тижня\b",
            lambda _: _format_date(ctx.reference_date + timedelta(days=7), ctx),
            text,
            flags=re.IGNORECASE | re.UNICODE,
        )
        text = re.sub(
            r"\bминулого\s+тижня\b",
            lambda _: _format_date(ctx.reference_date - timedelta(days=7), ctx),
            text,
            flags=re.IGNORECASE | re.UNICODE,
        )
        # "у п'ятницю" → next Friday from reference_date
        text = re.sub(
            r"\bу\s+([А-яёіїєґА-ЯЁІЇЄҐ']+)\b",
            lambda m: _resolve_weekday_uk(m, ctx),
            text,
            flags=re.UNICODE,
        )
    else:
        text = re.sub(
            r"\bnext\s+week\b",
            lambda _: _format_date(ctx.reference_date + timedelta(days=7), ctx),
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\blast\s+week\b",
            lambda _: _format_date(ctx.reference_date - timedelta(days=7), ctx),
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\bon\s+([A-Za-z]+)\b",
            lambda m: _resolve_weekday_en(m, ctx),
            text,
            flags=re.IGNORECASE,
        )

    return text, warnings


def _resolve_weekday_uk(m: re.Match[str], ctx: ProcessingContext) -> str:
    word = m.group(1).lower()
    wd = _WEEKDAYS_UK.get(word)
    if wd is None:
        return m.group(0)
    delta = (wd - ctx.reference_date.weekday()) % 7
    if delta == 0:
        delta = 7
    return _format_date(ctx.reference_date + timedelta(days=delta), ctx)


def _resolve_weekday_en(m: re.Match[str], ctx: ProcessingContext) -> str:
    word = m.group(1).lower()
    wd = _WEEKDAYS_EN.get(word)
    if wd is None:
        return m.group(0)
    delta = (wd - ctx.reference_date.weekday()) % 7
    if delta == 0:
        delta = 7
    return _format_date(ctx.reference_date + timedelta(days=delta), ctx)


# ── Absolute ────────────────────────────────────────────────────────


_ABS_UK = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_MONTHS_UK) + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE | re.UNICODE,
)
# Spelled ordinal day + month genitive: "третього травня [2026]". Longest
# phrase first so "двадцять першого" wins over a bare "першого".
_ABS_UK_ORD = re.compile(
    r"\b("
    + "|".join(sorted(_ORD_DAYS_UK, key=len, reverse=True))
    + r")\s+("
    + "|".join(_MONTHS_UK)
    + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE | re.UNICODE,
)
_ABS_EN = re.compile(
    r"\b(" + "|".join(_MONTHS_EN) + r")\s+(\d{1,2})(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_NUMERIC = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")


def _apply_absolute(text: str, ctx: ProcessingContext) -> tuple[str, list[PipelineWarning]]:
    warnings: list[PipelineWarning] = []
    if ctx.language == "uk":
        pattern = _ABS_UK

        def _conv(m: re.Match[str]) -> str:
            d = int(m.group(1))
            mo = _MONTHS_UK[m.group(2).lower()]
            y = int(m.group(3)) if m.group(3) else ctx.reference_date.year
            return _safe_format(d, mo, y, ctx, warnings)

        # Spelled ordinal day form runs first (disjoint from the digit form).
        def _conv_ord(m: re.Match[str]) -> str:
            d = _ORD_DAYS_UK[m.group(1).lower()]
            mo = _MONTHS_UK[m.group(2).lower()]
            y = int(m.group(3)) if m.group(3) else ctx.reference_date.year
            return _safe_format(d, mo, y, ctx, warnings)

        text = _ABS_UK_ORD.sub(_conv_ord, text)
    else:
        pattern = _ABS_EN

        def _conv(m: re.Match[str]) -> str:
            mo = _MONTHS_EN[m.group(1).lower()]
            d = int(m.group(2))
            y = int(m.group(3)) if m.group(3) else ctx.reference_date.year
            return _safe_format(d, mo, y, ctx, warnings)

    text = pattern.sub(_conv, text)

    # Already-numeric form: validate and either keep or flag ambiguous.
    def _conv_num(m: re.Match[str]) -> str:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _safe_format(d, mo, y, ctx, warnings)

    text = _NUMERIC.sub(_conv_num, text)
    return text, warnings


def _safe_format(
    day: int,
    month: int,
    year: int,
    ctx: ProcessingContext,
    warnings: list[PipelineWarning],
) -> str:
    """If (day, month, year) is invalid, leave as-is + emit warning."""
    try:
        d = date(year, month, day)
    except ValueError:
        warnings.append(
            PipelineWarning(
                code="ambiguous_date",
                detail=f"day={day} month={month} year={year} is not a valid date",
                stage="date_norm",
            )
        )
        return f"{day:02d}.{month:02d}.{year}"
    return _format_date(d, ctx)


# ── Time ────────────────────────────────────────────────────────────


_TIME_EXPLICIT = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_TIME_HOUR_WORD_UK = re.compile(
    r"\bо\s+(\d{1,2})(?:\s+годині)?(?:\s+(\d{1,2})\s+хвилин)?",
    re.IGNORECASE | re.UNICODE,
)


def _apply_time(text: str, ctx: ProcessingContext) -> tuple[str, list[PipelineWarning]]:
    warnings: list[PipelineWarning] = []
    if ctx.language == "uk":

        def _conv(m: re.Match[str]) -> str:
            h = int(m.group(1))
            mi = int(m.group(2)) if m.group(2) else 0
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return f"{h:02d}:{mi:02d}"
            return m.group(0)

        text = _TIME_HOUR_WORD_UK.sub(_conv, text)
    return text, warnings


# ── Formatting ──────────────────────────────────────────────────────


def _format_date(d: date, ctx: ProcessingContext) -> str:
    fmt = ctx.date_format
    if fmt == "YYYY-MM-DD":
        return d.isoformat()
    if fmt == "WORD":
        if ctx.language == "uk":
            month = _MONTH_NAMES_UK.get(d.month, str(d.month))
            return f"{d.day} {month} {d.year}"
        month = _MONTH_NAMES_EN.get(d.month, str(d.month))
        return f"{month} {d.day}, {d.year}"
    return f"{d.day:02d}.{d.month:02d}.{d.year}"
