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
        decimal_separator="," if language == "uk" else ".",
        bp_separator="/",
        date_format="DD.MM.YYYY" if language == "uk" else "YYYY-MM-DD",
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
