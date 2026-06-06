"""Pydantic validation tests for the ReportContent shape."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from report_models import (
    Icd10Code,
    ReportContent,
    ReportSection,
    canonical_content_bytes,
    rendered_text_from_content,
)


def _minimal_content() -> ReportContent:
    return ReportContent(
        template_id=uuid4(),
        template_schema_version=1,
        title="Cardiology consult",
        sections=[
            ReportSection(section_key="chief_complaint", text="chest pain x 2h"),
            ReportSection(section_key="assessment", text="probable angina"),
        ],
    )


def test_minimal_content_roundtrips() -> None:
    c = _minimal_content()
    obj = c.model_dump(mode="json")
    c2 = ReportContent.model_validate(obj)
    assert c == c2


def test_section_keys_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        ReportContent(
            template_id=uuid4(),
            template_schema_version=1,
            sections=[
                ReportSection(section_key="x", text="a"),
                ReportSection(section_key="x", text="b"),
            ],
        )


def test_extra_keys_rejected() -> None:
    obj = {
        "template_id": str(uuid4()),
        "template_schema_version": 1,
        "title": "",
        "sections": [],
        "icd10_codes": [],
        "encounter_date": None,
        "unknown_key": "explode",
    }
    with pytest.raises(ValidationError):
        ReportContent.model_validate(obj)


def test_icd10_validates_format() -> None:
    Icd10Code(code="I21.0")
    Icd10Code(code="I21")
    with pytest.raises(ValidationError):
        Icd10Code(code="12.0")          # must start with letter
    with pytest.raises(ValidationError):
        Icd10Code(code="I21.")          # trailing dot


def test_icd10_uppercases_lowercase_input() -> None:
    # mode='before' validator normalises before pattern check.
    assert Icd10Code(code="i21").code == "I21"
    assert Icd10Code(code="i21.0").code == "I21.0"


def test_canonical_bytes_are_deterministic() -> None:
    c = _minimal_content()
    a = canonical_content_bytes(c)
    b = canonical_content_bytes(c)
    assert a == b
    # Key order in source JSON must not change canonical output.
    raw1 = c.model_dump(mode="json")
    shuffled = {k: raw1[k] for k in sorted(raw1.keys(), reverse=True)}
    c2 = ReportContent.model_validate(shuffled)
    assert canonical_content_bytes(c2) == a


def test_canonical_bytes_change_with_content() -> None:
    c1 = _minimal_content()
    c2 = ReportContent.model_validate(json.loads(canonical_content_bytes(c1)))
    c2.sections[0].text = "different"
    assert canonical_content_bytes(c2) != canonical_content_bytes(c1)


def test_rendered_text_concatenates_sections_with_section_keys() -> None:
    c = _minimal_content()
    rt = rendered_text_from_content(c)
    assert "Cardiology consult" in rt
    assert "chief_complaint" in rt
    assert "chest pain x 2h" in rt
    assert "assessment" in rt
    assert "probable angina" in rt


def test_rendered_text_skips_empty_sections() -> None:
    c = ReportContent(
        template_id=uuid4(),
        template_schema_version=1,
        title="t",
        sections=[
            ReportSection(section_key="empty", text=""),
            ReportSection(section_key="present", text="content"),
        ],
    )
    rt = rendered_text_from_content(c)
    assert "empty" not in rt
    assert "present" in rt
