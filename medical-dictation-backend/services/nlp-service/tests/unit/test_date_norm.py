"""Date normalization fixtures (relative + absolute + ambiguous flag)."""

from __future__ import annotations

import asyncio
from datetime import date

from nlp_service.pipeline.base import (
    AbbreviationSnapshot,
    ProcessingContext,
    StageInput,
)
from nlp_service.stages.date_norm import DateNormStage


def _ctx(language: str, ref: date) -> ProcessingContext:
    return ProcessingContext(
        tenant_id=__import__("uuid").UUID("00000000-0000-0000-0000-000000000001"),
        language=language,
        specialty=None,
        reference_date=ref,
        is_partial=False,
        abbreviation_snapshot=AbbreviationSnapshot(entries=(), fingerprint="x"),
        pipeline_version="test",
        decimal_separator="," if language in {"uk", "de"} else ".",
        bp_separator="/",
        date_format="DD.MM.YYYY" if language in {"uk", "de"} else "YYYY-MM-DD",
    )


async def _run(stage: DateNormStage, ctx: ProcessingContext, text: str) -> str:
    out = await stage.process(ctx, StageInput(text=text))
    return out.text


def test_today_uk() -> None:
    stage = DateNormStage()
    out = asyncio.run(_run(stage, _ctx("uk", date(2026, 6, 15)), "сьогодні відвідав"))
    assert "15.06.2026" in out


def test_yesterday_uk() -> None:
    stage = DateNormStage()
    out = asyncio.run(_run(stage, _ctx("uk", date(2026, 6, 15)), "вчора зробив"))
    assert "14.06.2026" in out


def test_today_en_iso() -> None:
    stage = DateNormStage()
    out = asyncio.run(_run(stage, _ctx("en", date(2026, 6, 15)), "today checked"))
    assert "2026-06-15" in out


def test_absolute_uk() -> None:
    stage = DateNormStage()
    out = asyncio.run(_run(stage, _ctx("uk", date(2026, 6, 15)), "1 травня 2026"))
    assert "01.05.2026" in out


def test_spelled_ordinal_day_uk() -> None:
    # The sprint-05 headline example: "третього травня" → "03.05.2026"
    # (year supplied by reference_date).
    stage = DateNormStage()
    out = asyncio.run(_run(stage, _ctx("uk", date(2026, 6, 15)), "оглянуто третього травня"))
    assert "03.05.2026" in out


def test_spelled_ordinal_compound_day_uk() -> None:
    stage = DateNormStage()
    out = asyncio.run(_run(stage, _ctx("uk", date(2026, 1, 1)), "двадцять першого грудня 2025"))
    assert "21.12.2025" in out


def test_ambiguous_date_emits_warning() -> None:
    stage = DateNormStage()
    ctx = _ctx("uk", date(2026, 6, 15))
    coro = stage.process(ctx, StageInput(text="оглянуто 31.04.2026"))
    out = asyncio.run(coro)
    assert any(w.code == "ambiguous_date" for w in out.warnings)


def test_next_week_en() -> None:
    stage = DateNormStage()
    out = asyncio.run(_run(stage, _ctx("en", date(2026, 6, 15)), "see you next week"))
    assert "2026-06-22" in out


# ── German ──────────────────────────────────────────────────────────


def test_today_de() -> None:
    stage = DateNormStage()
    out = asyncio.run(_run(stage, _ctx("de", date(2026, 6, 15)), "heute vorgestellt"))
    assert "15.06.2026" in out


def test_relative_days_de() -> None:
    stage = DateNormStage()
    ctx = _ctx("de", date(2026, 6, 15))
    assert "14.06.2026" in asyncio.run(_run(stage, ctx, "gestern gestürzt"))
    assert "13.06.2026" in asyncio.run(_run(stage, ctx, "vorgestern gestürzt"))
    assert "16.06.2026" in asyncio.run(_run(stage, ctx, "Kontrolle morgen"))
    assert "17.06.2026" in asyncio.run(_run(stage, ctx, "Kontrolle übermorgen"))


def test_morgen_as_morning_is_not_a_date() -> None:
    """ "morgen" is tomorrow; "am Morgen"/"morgen früh" is a time of day.
    Resolving the morning readings would move a finding by a day."""
    stage = DateNormStage()
    ctx = _ctx("de", date(2026, 6, 15))
    for text in ("am Morgen war der Blutdruck hoch", "morgen früh nüchtern", "guten Morgen"):
        assert asyncio.run(_run(stage, ctx, text)) == text


def test_absolute_de() -> None:
    stage = DateNormStage()
    out = asyncio.run(_run(stage, _ctx("de", date(2026, 6, 15)), "Aufnahme 5. März 2026"))
    assert "05.03.2026" in out


def test_absolute_de_year_from_reference_date() -> None:
    stage = DateNormStage()
    out = asyncio.run(_run(stage, _ctx("de", date(2026, 6, 15)), "Aufnahme 5 März"))
    assert "05.03.2026" in out


def test_spelled_ordinal_day_de() -> None:
    stage = DateNormStage()
    out = asyncio.run(_run(stage, _ctx("de", date(2026, 6, 15)), "Termin am fünften März"))
    assert "05.03.2026" in out


def test_spelled_ordinal_compound_day_de() -> None:
    stage = DateNormStage()
    out = asyncio.run(
        _run(stage, _ctx("de", date(2026, 1, 1)), "am einundzwanzigsten Dezember 2025")
    )
    assert "21.12.2025" in out


def test_week_and_weekday_de() -> None:
    stage = DateNormStage()
    ctx = _ctx("de", date(2026, 6, 15))  # a Monday
    assert "22.06.2026" in asyncio.run(_run(stage, ctx, "Kontrolle nächste Woche"))
    assert "08.06.2026" in asyncio.run(_run(stage, ctx, "letzte Woche Beschwerden"))
    assert "19.06.2026" in asyncio.run(_run(stage, ctx, "am Freitag Kontrolle"))


def test_clock_times_de() -> None:
    stage = DateNormStage()
    ctx = _ctx("de", date(2026, 6, 15))
    assert "08:30" in asyncio.run(_run(stage, ctx, "um 8 Uhr 30 nüchtern"))
    assert "08:00" in asyncio.run(_run(stage, ctx, "um 8 Uhr nüchtern"))


def test_halb_acht_is_seven_thirty() -> None:
    """German "halb acht" is HALF TO eight — 07:30. Reading it as the
    English "half past" would move every appointment by an hour."""
    stage = DateNormStage()
    ctx = _ctx("de", date(2026, 6, 15))
    assert "07:30" in asyncio.run(_run(stage, ctx, "halb acht"))
    assert "07:30" in asyncio.run(_run(stage, ctx, "halb 8"))
    assert "11:30" in asyncio.run(_run(stage, ctx, "halb zwölf"))
    # "halb eins" names the coming hour: 12:30, never 00:30.
    assert "12:30" in asyncio.run(_run(stage, ctx, "halb eins"))


def test_viertel_vor_nach_de() -> None:
    stage = DateNormStage()
    ctx = _ctx("de", date(2026, 6, 15))
    assert "09:45" in asyncio.run(_run(stage, ctx, "Viertel vor zehn"))
    assert "10:15" in asyncio.run(_run(stage, ctx, "Viertel nach zehn"))


def test_ambiguous_date_emits_warning_de() -> None:
    stage = DateNormStage()
    out = asyncio.run(
        DateNormStage().process(_ctx("de", date(2026, 6, 15)), StageInput(text="31.04.2026"))
    )
    assert any(w.code == "ambiguous_date" for w in out.warnings)
    assert stage.name == "date_norm"
