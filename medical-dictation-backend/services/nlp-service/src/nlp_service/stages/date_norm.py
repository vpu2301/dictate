"""Stage 4 — date & time normalization.

Two parsers operating on the post-Stage-3 text:

- **Relative**: "сьогодні"/"today"/"heute", "вчора"/"yesterday"/"gestern",
  "завтра"/"tomorrow"/"morgen", "у п'ятницю"/"on Friday"/"am Freitag",
  "наступного тижня"/"next week"/"nächste Woche", "минулого місяця"/"last month".
  Anchored to ``ctx.reference_date``.
- **Absolute**: "1 травня 2026", "May 1, 2026", "5. März 2026", "перше травня
  двадцять двадцять шостого" (spelled-out Ukrainian year), "am fünften März".

German carries two traps this module handles explicitly: "morgen" is
both *tomorrow* and *morning* (resolved only outside the morning
readings), and "halb acht" is 07:30, not 08:30.

Output respects ``ctx.date_format``:
- ``DD.MM.YYYY`` (default Ukrainian + German)
- ``YYYY-MM-DD`` (ISO)
- ``WORD`` (e.g., "1 травня 2026", "5. März 2026")

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
from .artifacts import date_artifacts_from_output

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
_WEEKDAYS_DE = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonnabend": 5,
    "sonntag": 6,
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

_MONTHS_DE = {
    "januar": 1,
    "jänner": 1,
    "februar": 2,
    "märz": 3,
    "maerz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}
_MONTH_NAMES_DE = {v: k.capitalize() for k, v in _MONTHS_DE.items() if k not in {"jänner", "maerz"}}

# Spelled German ordinal days, as dictated: "am fünften März", "dritter
# Mai". The four case endings (-te/-ter/-ten/-tes) are generated rather
# than listed so the table cannot go half-updated.
_ORD_STEMS_DE: dict[str, int] = {
    "erst": 1,
    "zweit": 2,
    "dritt": 3,
    "viert": 4,
    "fünft": 5,
    "sechst": 6,
    "siebt": 7,
    "siebent": 7,
    "acht": 8,
    "neunt": 9,
    "zehnt": 10,
    "elft": 11,
    "zwölft": 12,
    "dreizehnt": 13,
    "vierzehnt": 14,
    "fünfzehnt": 15,
    "sechzehnt": 16,
    "siebzehnt": 17,
    "achtzehnt": 18,
    "neunzehnt": 19,
    "zwanzigst": 20,
    "einundzwanzigst": 21,
    "zweiundzwanzigst": 22,
    "dreiundzwanzigst": 23,
    "vierundzwanzigst": 24,
    "fünfundzwanzigst": 25,
    "sechsundzwanzigst": 26,
    "siebenundzwanzigst": 27,
    "achtundzwanzigst": 28,
    "neunundzwanzigst": 29,
    "dreißigst": 30,
    "dreissigst": 30,
    "einunddreißigst": 31,
    "einunddreissigst": 31,
}


def _build_ordinal_days_de() -> dict[str, int]:
    out: dict[str, int] = {}
    for stem, day in _ORD_STEMS_DE.items():
        for ending in ("e", "er", "en", "es", "em"):
            out[stem + ending] = day
    return out


_ORD_DAYS_DE = _build_ordinal_days_de()

# Spelled hours for "halb acht" / "Viertel vor zehn".
_HOURS_DE: dict[str, int] = {
    "eins": 1,
    "ein": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
    "elf": 11,
    "zwölf": 12,
}


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
            numeric_artifacts=input.numeric_artifacts,
            date_artifacts=date_artifacts_from_output(new_text),
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
# "morgen" is BOTH "tomorrow" and "morning" ("am Morgen", "morgen früh",
# "guten Morgen"). Case can't disambiguate it — the punctuation model
# capitalizes unreliably — so the morning readings are excluded by
# context below and only the bare adverb is resolved to a date.
_REL_DE = {
    "heute": 0,
    "gestern": -1,
    "vorgestern": -2,
    "morgen": 1,
    "übermorgen": 2,
}
_DE_MORNING_BEFORE = r"(?<!\bam )(?<!\bguten )(?<!\bjeden )(?<!\bdiesen )(?<!\bheute )"
_DE_MORNING_AFTER = r"(?!\s+(?:früh|abend|mittag|nachmittag))"


_REL_TABLES = {"uk": _REL_UK, "en": _REL_EN, "de": _REL_DE}


def _apply_relative(text: str, ctx: ProcessingContext) -> tuple[str, list[PipelineWarning]]:
    warnings: list[PipelineWarning] = []
    table = _REL_TABLES.get(ctx.language, _REL_EN)

    def _replace_simple(m: re.Match[str]) -> str:
        word = m.group(0).lower()
        offset = table.get(word)
        if offset is None:
            return m.group(0)
        d = ctx.reference_date + timedelta(days=offset)
        return _format_date(d, ctx)

    if ctx.language == "de":
        # "morgen" only counts as a date away from the morning readings.
        pattern = re.compile(
            _DE_MORNING_BEFORE
            + r"\b("
            + "|".join(re.escape(k) for k in table)
            + r")\b"
            + _DE_MORNING_AFTER,
            re.IGNORECASE | re.UNICODE,
        )
    else:
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
    elif ctx.language == "de":
        text = re.sub(
            r"\b(?:nächste[nrs]?|kommende[nrs]?)\s+Woche\b",
            lambda _: _format_date(ctx.reference_date + timedelta(days=7), ctx),
            text,
            flags=re.IGNORECASE | re.UNICODE,
        )
        text = re.sub(
            r"\b(?:letzte[nrs]?|vergangene[nrs]?|vorige[nrs]?)\s+Woche\b",
            lambda _: _format_date(ctx.reference_date - timedelta(days=7), ctx),
            text,
            flags=re.IGNORECASE | re.UNICODE,
        )
        # "am Freitag" → the coming Friday.
        text = re.sub(
            r"\bam\s+([A-Za-zÄÖÜäöüß]+)\b",
            lambda m: _resolve_weekday_de(m, ctx),
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


def _resolve_weekday_de(m: re.Match[str], ctx: ProcessingContext) -> str:
    word = m.group(1).lower()
    wd = _WEEKDAYS_DE.get(word)
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
# German: "5. März 2026", "5 März", plus the spelled ordinal form
# "am fünften März". Longest ordinal first so "einundzwanzigsten" wins
# over "ersten".
_ABS_DE = re.compile(
    r"\b(\d{1,2})\.?\s+(" + "|".join(_MONTHS_DE) + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE | re.UNICODE,
)
_ABS_DE_ORD = re.compile(
    r"\b("
    + "|".join(sorted(_ORD_DAYS_DE, key=len, reverse=True))
    + r")\s+("
    + "|".join(_MONTHS_DE)
    + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE | re.UNICODE,
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
    elif ctx.language == "de":
        pattern = _ABS_DE

        def _conv(m: re.Match[str]) -> str:
            d = int(m.group(1))
            mo = _MONTHS_DE[m.group(2).lower()]
            y = int(m.group(3)) if m.group(3) else ctx.reference_date.year
            return _safe_format(d, mo, y, ctx, warnings)

        def _conv_ord_de(m: re.Match[str]) -> str:
            d = _ORD_DAYS_DE[m.group(1).lower()]
            mo = _MONTHS_DE[m.group(2).lower()]
            y = int(m.group(3)) if m.group(3) else ctx.reference_date.year
            return _safe_format(d, mo, y, ctx, warnings)

        text = _ABS_DE_ORD.sub(_conv_ord_de, text)
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
# "um 8 Uhr", "um 8 Uhr 30". "Uhr" is required — a bare "um 8" is as
# often a dose interval as a clock time.
_TIME_HOUR_WORD_DE = re.compile(
    r"\bum\s+(\d{1,2})\s+Uhr(?:\s+(\d{1,2}))?",
    re.IGNORECASE | re.UNICODE,
)
_HOUR_ALT = "|".join(sorted(_HOURS_DE, key=len, reverse=True))
# "halb acht" is 07:30 — HALF TO the named hour, not half past it. The
# English reading (08:30) is the single most expensive mistranslation in
# this file: it silently moves an appointment or a dose by an hour.
_TIME_HALF_DE = re.compile(rf"\bhalb\s+(\d{{1,2}}|{_HOUR_ALT})\b", re.IGNORECASE | re.UNICODE)
_TIME_QUARTER_DE = re.compile(
    rf"\bviertel\s+(vor|nach)\s+(\d{{1,2}}|{_HOUR_ALT})\b", re.IGNORECASE | re.UNICODE
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
    elif ctx.language == "de":

        def _conv_de(m: re.Match[str]) -> str:
            h = int(m.group(1))
            mi = int(m.group(2)) if m.group(2) else 0
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return f"{h:02d}:{mi:02d}"
            return m.group(0)

        def _conv_half(m: re.Match[str]) -> str:
            h = _hour_value_de(m.group(1))
            if h is None:
                return m.group(0)
            return f"{_hour_before_de(h):02d}:30"

        def _conv_quarter(m: re.Match[str]) -> str:
            h = _hour_value_de(m.group(2))
            if h is None:
                return m.group(0)
            if m.group(1).lower() == "vor":
                return f"{_hour_before_de(h):02d}:45"
            return f"{h % 24:02d}:15"

        text = _TIME_HOUR_WORD_DE.sub(_conv_de, text)
        text = _TIME_HALF_DE.sub(_conv_half, text)
        text = _TIME_QUARTER_DE.sub(_conv_quarter, text)
    return text, warnings


def _hour_value_de(token: str) -> int | None:
    if token.isdigit():
        h = int(token)
        return h if 0 <= h <= 23 else None
    return _HOURS_DE.get(token.lower())


def _hour_before_de(hour: int) -> int:
    """The hour "halb"/"Viertel vor" counts down from.

    "halb eins" is 12:30, not 00:30 — a spoken 12-hour clock names the
    coming hour, and midday is overwhelmingly the intended one in a
    consultation. (Every spelled hour is 12-hour ambiguous either way;
    this picks the same daytime reading the UK/EN paths already do.)
    """
    return hour - 1 if hour > 1 else 12


# ── Formatting ──────────────────────────────────────────────────────


def _format_date(d: date, ctx: ProcessingContext) -> str:
    fmt = ctx.date_format
    if fmt == "YYYY-MM-DD":
        return d.isoformat()
    if fmt == "WORD":
        if ctx.language == "uk":
            month = _MONTH_NAMES_UK.get(d.month, str(d.month))
            return f"{d.day} {month} {d.year}"
        if ctx.language == "de":
            month = _MONTH_NAMES_DE.get(d.month, str(d.month))
            return f"{d.day}. {month} {d.year}"
        month = _MONTH_NAMES_EN.get(d.month, str(d.month))
        return f"{month} {d.day}, {d.year}"
    return f"{d.day:02d}.{d.month:02d}.{d.year}"
