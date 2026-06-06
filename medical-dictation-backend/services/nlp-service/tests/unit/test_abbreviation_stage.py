"""Abbreviation stage tests — direction, word-boundary, tenant override."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from nlp_service.pipeline.base import (
    AbbreviationEntry,
    AbbreviationSnapshot,
    ProcessingContext,
    StageInput,
)
from nlp_service.stages.abbreviation import AbbreviationStage


def _snapshot(entries: list[AbbreviationEntry]) -> AbbreviationSnapshot:
    return AbbreviationSnapshot(entries=tuple(entries), fingerprint="t")


def _ctx(language: str, snap: AbbreviationSnapshot, specialty: str | None = None) -> ProcessingContext:
    from datetime import date

    return ProcessingContext(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        language=language,
        specialty=specialty,
        reference_date=date(2026, 1, 1),
        is_partial=False,
        abbreviation_snapshot=snap,
        pipeline_version="t",
    )


async def _run(stage: AbbreviationStage, ctx: ProcessingContext, text: str) -> str:
    out = await stage.process(ctx, StageInput(text=text))
    return out.text


def test_compact_direction_uk() -> None:
    stage = AbbreviationStage()
    snap = _snapshot([
        AbbreviationEntry(
            expanded="інфаркт міокарда",
            abbreviated="ІМ",
            direction="compact",
            domain="cardiology",
            case_sensitive=True,
            is_tenant_override=False,
        ),
    ])
    out = asyncio.run(_run(stage, _ctx("uk", snap, "cardiology"),
                           "перенесений інфаркт міокарда у 2020"))
    assert "ІМ" in out


def test_word_boundary_prevents_in_word_substitution() -> None:
    stage = AbbreviationStage()
    snap = _snapshot([
        AbbreviationEntry(
            expanded="ІМ",
            abbreviated="інфаркт міокарда",
            direction="expand",
            domain=None,
            case_sensitive=True,
            is_tenant_override=False,
        ),
    ])
    # "імпорт" contains "ІМ" as a substring but not as a word boundary.
    out = asyncio.run(_run(stage, _ctx("uk", snap), "імпорт даних"))
    assert out == "імпорт даних"


def test_tenant_override_wins() -> None:
    stage = AbbreviationStage()
    snap = _snapshot([
        AbbreviationEntry(
            expanded="артеріальний тиск",
            abbreviated="АТ",
            direction="compact",
            domain=None,
            case_sensitive=True,
            is_tenant_override=False,
        ),
        AbbreviationEntry(
            expanded="артеріальний тиск",
            abbreviated="АТ",
            direction="expand",  # tenant prefers expansion
            domain=None,
            case_sensitive=True,
            is_tenant_override=True,
        ),
    ])
    # Tenant rule says "expand" → input "АТ" → "артеріальний тиск".
    out = asyncio.run(_run(stage, _ctx("uk", snap),
                           "АТ 120/80 мм рт. ст."))
    assert "артеріальний тиск" in out


def test_direction_either_passes_through() -> None:
    stage = AbbreviationStage()
    snap = _snapshot([
        AbbreviationEntry(
            expanded="електрокардіографія",
            abbreviated="ЕКГ",
            direction="either",
            domain="all",
            case_sensitive=True,
            is_tenant_override=False,
        ),
    ])
    out = asyncio.run(_run(stage, _ctx("uk", snap), "зняли ЕКГ"))
    assert out == "зняли ЕКГ"
