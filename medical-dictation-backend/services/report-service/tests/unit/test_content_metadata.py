"""Sprint-13 write-path field-metadata validation tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException

from report_models import ReportContent
from report_service.domain import content_metadata
from report_service.domain.content_metadata import (
    check_content_metadata,
    validate_content_metadata,
)
from report_service.routers._content_guard import ensure_valid_field_metadata
from template_models import ChoiceOption, FieldType, TemplateDefinition, TemplateSection


def _template() -> TemplateDefinition:
    return TemplateDefinition(
        code="anamnesis_intake",
        name="Анамнез",
        language="uk",
        specialty="family_medicine",
        sections=(
            TemplateSection(
                id="complaints",
                name="Скарги",
                voice_aliases=("скарги",),
                asr_prompt="скарги",
            ),
            TemplateSection(
                id="smoking_status",
                name="Куріння",
                voice_aliases=("куріння",),
                field_type=FieldType.CHOICE,
                asr_prompt="статус куріння",
                options=(
                    ChoiceOption(value="never", label="Не палить"),
                    ChoiceOption(value="current", label="Палить"),
                ),
            ),
            TemplateSection(
                id="allergies",
                name="Алергії",
                voice_aliases=("алергії",),
                field_type=FieldType.MULTI_CHOICE,
                asr_prompt="алергії",
                options=(
                    ChoiceOption(value="none_known", label="Не відомі"),
                    ChoiceOption(value="latex", label="Латекс"),
                ),
            ),
        ),
    )


def _content(sections: list[dict[str, Any]]) -> ReportContent:
    return ReportContent.model_validate(
        {
            "template_id": "70cd91de-82b0-48e5-81ce-dcc01e0a2297",
            "template_schema_version": 1,
            "sections": sections,
        }
    )


def test_valid_choice_metadata_passes() -> None:
    content = _content(
        [
            {
                "section_key": "smoking_status",
                "text": "не палить",
                "field_specific_metadata": {
                    "selected": "never",
                    "confidence": 0.9,
                    "source": "extracted",
                },
            }
        ]
    )
    assert validate_content_metadata(content, _template()) == []


def test_valid_multi_choice_metadata_passes() -> None:
    content = _content(
        [
            {
                "section_key": "allergies",
                "text": "латекс",
                "field_specific_metadata": {"selected": ["latex"], "source": "manual"},
            }
        ]
    )
    assert validate_content_metadata(content, _template()) == []


def test_empty_metadata_everywhere_passes() -> None:
    content = _content([{"section_key": "complaints", "text": "болить голова"}])
    assert validate_content_metadata(content, _template()) == []


def test_unknown_selected_value_flagged() -> None:
    content = _content(
        [
            {
                "section_key": "smoking_status",
                "text": "",
                "field_specific_metadata": {"selected": "vaping", "source": "manual"},
            }
        ]
    )
    problems = validate_content_metadata(content, _template())
    assert [p.code for p in problems] == ["choice_value_unknown"]
    assert problems[0].section_key == "smoking_status"
    assert "vaping" in problems[0].reason


def test_unknown_multi_choice_value_flagged_per_value() -> None:
    content = _content(
        [
            {
                "section_key": "allergies",
                "text": "",
                "field_specific_metadata": {
                    "selected": ["latex", "dust", "pollen"],
                    "source": "manual",
                },
            }
        ]
    )
    problems = validate_content_metadata(content, _template())
    assert [p.code for p in problems] == ["choice_value_unknown", "choice_value_unknown"]


def test_invalid_shape_flagged_with_reason() -> None:
    content = _content(
        [
            {
                "section_key": "smoking_status",
                "text": "",
                "field_specific_metadata": {"selected": "never"},  # no source
            }
        ]
    )
    problems = validate_content_metadata(content, _template())
    assert [p.code for p in problems] == ["field_metadata_invalid"]
    assert "source" in problems[0].reason


def test_metadata_on_free_text_section_flagged() -> None:
    content = _content(
        [
            {
                "section_key": "complaints",
                "text": "x",
                "field_specific_metadata": {"selected": "never", "source": "manual"},
            }
        ]
    )
    problems = validate_content_metadata(content, _template())
    assert [p.code for p in problems] == ["field_metadata_invalid"]


def test_metadata_on_section_not_in_template_flagged() -> None:
    content = _content(
        [
            {
                "section_key": "ghost",
                "text": "x",
                "field_specific_metadata": {"selected": "never", "source": "manual"},
            }
        ]
    )
    problems = validate_content_metadata(content, _template())
    assert [p.code for p in problems] == ["field_metadata_invalid"]
    assert problems[0].section_key == "ghost"


# ── check_content_metadata (template resolution) ────────────────────


@pytest.mark.asyncio
async def test_fast_path_skips_template_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(conn: object, *, template_id: object) -> None:
        raise AssertionError("template must not be fetched when no metadata present")

    monkeypatch.setattr(content_metadata.repository, "get_template", _boom)
    content = _content([{"section_key": "complaints", "text": "x"}])
    assert await check_content_metadata(object(), content=content) == []


@pytest.mark.asyncio
async def test_missing_template_rejects_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(conn: object, *, template_id: object) -> None:
        return None

    monkeypatch.setattr(content_metadata.repository, "get_template", _none)
    content = _content(
        [
            {
                "section_key": "smoking_status",
                "text": "",
                "field_specific_metadata": {"selected": "never", "source": "manual"},
            }
        ]
    )
    problems = await check_content_metadata(object(), content=content)
    assert [p.code for p in problems] == ["field_metadata_invalid"]
    assert "template not found" in problems[0].reason


@pytest.mark.asyncio
async def test_happy_path_resolves_template_row(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"schema_jsonb": json.dumps(_template().model_dump(mode="json"))}

    async def _row(conn: object, *, template_id: object) -> dict[str, Any]:
        return row

    monkeypatch.setattr(content_metadata.repository, "get_template", _row)
    content = _content(
        [
            {
                "section_key": "smoking_status",
                "text": "",
                "field_specific_metadata": {"selected": "never", "source": "manual"},
            }
        ]
    )
    assert await check_content_metadata(object(), content=content) == []


# ── the router guard (422 contract) ─────────────────────────────────


@pytest.mark.asyncio
async def test_guard_raises_422_with_section_addressed_problems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {"schema_jsonb": json.dumps(_template().model_dump(mode="json"))}

    async def _row(conn: object, *, template_id: object) -> dict[str, Any]:
        return row

    monkeypatch.setattr(content_metadata.repository, "get_template", _row)
    content = _content(
        [
            {
                "section_key": "smoking_status",
                "text": "",
                "field_specific_metadata": {"selected": "vaping", "source": "manual"},
            }
        ]
    )
    with pytest.raises(HTTPException) as exc:
        await ensure_valid_field_metadata(object(), content=content)
    assert exc.value.status_code == 422
    extras = exc.value.problem_extras  # type: ignore[attr-defined]
    assert extras["code"] == "choice_value_unknown"
    assert extras["problems"][0]["section_key"] == "smoking_status"


@pytest.mark.asyncio
async def test_guard_passes_valid_content(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"schema_jsonb": json.dumps(_template().model_dump(mode="json"))}

    async def _row(conn: object, *, template_id: object) -> dict[str, Any]:
        return row

    monkeypatch.setattr(content_metadata.repository, "get_template", _row)
    content = _content(
        [
            {
                "section_key": "smoking_status",
                "text": "",
                "field_specific_metadata": {
                    "selected": "never",
                    "confidence": 0.8,
                    "source": "extracted",
                },
            }
        ]
    )
    await ensure_valid_field_metadata(object(), content=content)  # must not raise
