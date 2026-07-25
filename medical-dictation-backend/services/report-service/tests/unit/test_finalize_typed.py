"""Typed finalize completeness + the confirmation flag (sprint 13, step 06).

The authority rule under test throughout: extractor PROPOSALS never
satisfy a diagnosis, under any flag value. A finalized — and therefore
signable — report must never carry a machine-chosen diagnosis.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from report_models import Icd10Code, ReportContent, ReportSection
from report_service.domain.field_audit import diff_field_events
from report_service.domain.finalize_validator import validate_finalize
from template_models import ChoiceOption, FieldType, TemplateDefinition, TemplateSection

_PATIENT = uuid4()


def _tpl(*sections: TemplateSection) -> TemplateDefinition:
    return TemplateDefinition(
        code="anamnesis_intake",
        name="Анамнез",
        language="uk",
        specialty="family_medicine",
        sections=sections,
    )


def _choice_section(required: bool = True) -> TemplateSection:
    return TemplateSection(
        id="smoking_status",
        name="Куріння",
        voice_aliases=("куріння",),
        required=required,
        field_type=FieldType.CHOICE,
        asr_prompt="куріння",
        options=(
            ChoiceOption(value="never", label="Не палить"),
            ChoiceOption(value="current", label="Палить"),
        ),
    )


def _diagnosis_section(required: bool = True) -> TemplateSection:
    return TemplateSection(
        id="diagnosis",
        name="Діагноз",
        voice_aliases=("діагноз",),
        required=required,
        field_type=FieldType.STRUCTURED_DIAGNOSIS,
        asr_prompt="діагноз",
    )


def _numeric_section() -> TemplateSection:
    return TemplateSection(
        id="temperature",
        name="Температура",
        voice_aliases=("температура",),
        field_type=FieldType.NUMERIC_WITH_UNIT,
        asr_prompt="температура",
    )


def _date_section(field_type: FieldType = FieldType.DATE, min_chars: int = 0) -> TemplateSection:
    return TemplateSection(
        id="onset",
        name="Початок",
        voice_aliases=("початок",),
        field_type=field_type,
        asr_prompt="початок",
        min_chars=min_chars,
    )


def _content(*sections: ReportSection) -> ReportContent:
    return ReportContent(template_id=uuid4(), template_schema_version=1, sections=list(sections))


def _section(
    key: str, *, text: str = "", meta: dict[str, Any] | None = None, icd: list[str] | None = None
) -> ReportSection:
    return ReportSection(
        section_key=key,
        text=text,
        field_specific_metadata=meta or {},
        icd10=[Icd10Code(code=c) for c in (icd or [])],
    )


def _codes(problems: list[Any]) -> list[str]:
    return [p.code for p in problems]


# ── the VERIFY trio ─────────────────────────────────────────────────


def test_required_choice_without_selection_blocks() -> None:
    problems = validate_finalize(
        content=_content(_section("smoking_status")),
        template=_tpl(_choice_section()),
        patient_id=_PATIENT,
    )
    assert _codes(problems) == ["choice_not_selected"]
    assert problems[0].section_key == "smoking_status"
    assert problems[0].reason == "choice_not_selected"


def test_extracted_unconfirmed_diagnosis_blocks_with_the_flag_on() -> None:
    content = _content(
        _section(
            "diagnosis",
            text="гіпертонічна хвороба",
            meta={
                "proposals": [{"code": "I10", "display": "Гіпертензія", "confidence": 0.9}],
                "confidence": 0.9,
                "source": "extracted",
            },
        )
    )
    problems = validate_finalize(
        content=content,
        template=_tpl(_diagnosis_section()),
        patient_id=_PATIENT,
        require_confirmed_diagnosis=True,
    )
    assert _codes(problems) == ["diagnosis_not_confirmed"]


def test_confirmed_diagnosis_passes() -> None:
    content = _content(_section("diagnosis", text="гіпертонічна хвороба", icd=["I10"]))
    problems = validate_finalize(
        content=content, template=_tpl(_diagnosis_section()), patient_id=_PATIENT
    )
    assert problems == []


# ── the authority rule ──────────────────────────────────────────────


@pytest.mark.parametrize("flag", [True, False])
def test_proposals_never_satisfy_a_diagnosis(flag: bool) -> None:
    """Under EVERY flag value, proposals alone leave the section unfilled."""
    content = _content(
        _section(
            "diagnosis",
            meta={
                "proposals": [{"code": "I10", "confidence": 0.99}],
                "confidence": 0.99,
                "source": "extracted",
            },
        )
    )
    problems = validate_finalize(
        content=content,
        template=_tpl(_diagnosis_section()),
        patient_id=_PATIENT,
        require_confirmed_diagnosis=flag,
    )
    assert len(problems) == 1, "a proposal must never count as a confirmed diagnosis"
    assert problems[0].code in {"diagnosis_not_confirmed", "missing_icd10"}


def test_flag_off_reports_missing_icd10_instead() -> None:
    """The flag changes MESSAGING, never the authority rule."""
    content = _content(
        _section(
            "diagnosis",
            meta={
                "proposals": [{"code": "I10", "confidence": 0.99}],
                "confidence": 0.99,
                "source": "extracted",
            },
        )
    )
    problems = validate_finalize(
        content=content,
        template=_tpl(_diagnosis_section()),
        patient_id=_PATIENT,
        require_confirmed_diagnosis=False,
    )
    assert _codes(problems) == ["missing_icd10"]


def test_no_proposals_and_no_codes_is_missing_icd10_under_both_flags() -> None:
    for flag in (True, False):
        problems = validate_finalize(
            content=_content(_section("diagnosis")),
            template=_tpl(_diagnosis_section()),
            patient_id=_PATIENT,
            require_confirmed_diagnosis=flag,
        )
        assert _codes(problems) == ["missing_icd10"], flag


# ── the filled matrix ───────────────────────────────────────────────


@pytest.mark.parametrize("source", ["extracted", "manual"])
def test_choice_filled_by_either_source(source: str) -> None:
    meta: dict[str, Any] = {"selected": "never", "source": source}
    if source == "extracted":
        meta["confidence"] = 0.9
    problems = validate_finalize(
        content=_content(_section("smoking_status", meta=meta)),
        template=_tpl(_choice_section()),
        patient_id=_PATIENT,
    )
    assert problems == []


def test_multi_choice_empty_selection_blocks() -> None:
    section = _choice_section()
    multi = section.model_copy(update={"field_type": FieldType.MULTI_CHOICE})
    problems = validate_finalize(
        content=_content(_section("smoking_status", meta={})),
        template=_tpl(multi),
        patient_id=_PATIENT,
    )
    assert _codes(problems) == ["choice_not_selected"]


def test_optional_typed_section_never_blocks() -> None:
    problems = validate_finalize(
        content=_content(_section("smoking_status")),
        template=_tpl(_choice_section(required=False)),
        patient_id=_PATIENT,
    )
    assert problems == []


def test_optional_diagnosis_never_blocks() -> None:
    problems = validate_finalize(
        content=_content(_section("diagnosis")),
        template=_tpl(_diagnosis_section(required=False)),
        patient_id=_PATIENT,
    )
    assert problems == []


def test_numeric_requires_both_value_and_unit() -> None:
    tpl = _tpl(_numeric_section())
    filled = _content(
        _section("temperature", meta={"value": 37.2, "unit": "°C", "source": "manual"})
    )
    assert validate_finalize(content=filled, template=tpl, patient_id=_PATIENT) == []

    no_unit = _content(_section("temperature", meta={"value": 37.2, "source": "manual"}))
    assert _codes(validate_finalize(content=no_unit, template=tpl, patient_id=_PATIENT)) == [
        "numeric_not_filled"
    ]

    empty = _content(_section("temperature"))
    assert _codes(validate_finalize(content=empty, template=tpl, patient_id=_PATIENT)) == [
        "numeric_not_filled"
    ]


def test_date_requires_a_date() -> None:
    tpl = _tpl(_date_section())
    filled = _content(_section("onset", meta={"date": "2026-07-01", "source": "manual"}))
    assert validate_finalize(content=filled, template=tpl, patient_id=_PATIENT) == []
    assert _codes(
        validate_finalize(content=_content(_section("onset")), template=tpl, patient_id=_PATIENT)
    ) == ["date_not_filled"]


def test_date_with_note_also_applies_min_chars_to_the_prose() -> None:
    """The note IS content, so min_chars still governs it."""
    tpl = _tpl(_date_section(FieldType.DATE_WITH_NOTE, min_chars=10))
    short = _content(
        _section("onset", text="коротко", meta={"date": "2026-07-01", "source": "manual"})
    )
    assert "below_min_chars" in _codes(
        validate_finalize(content=short, template=tpl, patient_id=_PATIENT)
    )
    ok = _content(
        _section(
            "onset",
            text="почалося минулого тижня після застуди",
            meta={"date": "2026-07-01", "source": "manual"},
        )
    )
    assert validate_finalize(content=ok, template=tpl, patient_id=_PATIENT) == []


def test_free_text_sections_keep_sprint_08_behaviour() -> None:
    free = TemplateSection(
        id="complaints",
        name="Скарги",
        voice_aliases=("скарги",),
        required=True,
        asr_prompt="скарги",
        min_chars=20,
    )
    short = _content(_section("complaints", text="мало"))
    assert _codes(validate_finalize(content=short, template=_tpl(free), patient_id=_PATIENT)) == [
        "below_min_chars"
    ]
    missing = _content()
    assert _codes(validate_finalize(content=missing, template=_tpl(free), patient_id=_PATIENT)) == [
        "missing_required_section"
    ]


def test_typed_sections_never_emit_min_chars_or_required_empty() -> None:
    """A choice section has no prose, so the prose codes must not fire."""
    codes = _codes(
        validate_finalize(
            content=_content(_section("smoking_status")),
            template=_tpl(_choice_section()),
            patient_id=_PATIENT,
        )
    )
    assert "missing_required_section" not in codes
    assert "below_min_chars" not in codes


# ── audit signals: the PHI line ─────────────────────────────────────


def _tpl_types() -> dict[str, str]:
    return {
        "smoking_status": "choice",
        "diagnosis": "structured_diagnosis",
        "complaints": "free_text",
    }


def test_confirming_an_extracted_choice_emits_confirmed() -> None:
    before = _content(
        _section(
            "smoking_status", meta={"selected": "never", "confidence": 0.9, "source": "extracted"}
        )
    )
    after = _content(_section("smoking_status", meta={"selected": "never", "source": "manual"}))
    events = diff_field_events(before=before, after=after, field_types=_tpl_types())
    assert [e.kind for e in events] == ["confirmed"]
    assert events[0].payload["selected"] == ["never"]


def test_changing_an_extracted_choice_emits_overridden() -> None:
    before = _content(
        _section(
            "smoking_status", meta={"selected": "never", "confidence": 0.9, "source": "extracted"}
        )
    )
    after = _content(_section("smoking_status", meta={"selected": "current", "source": "manual"}))
    events = diff_field_events(before=before, after=after, field_types=_tpl_types())
    assert [e.kind for e in events] == ["overridden"]
    assert events[0].payload["selected"] == ["current"]
    assert events[0].payload["was"] == ["never"]


def test_confirming_a_proposed_code_emits_confirmed() -> None:
    before = _content(
        _section(
            "diagnosis",
            meta={
                "proposals": [{"code": "I10", "confidence": 0.9}],
                "confidence": 0.9,
                "source": "extracted",
            },
        )
    )
    after = _content(_section("diagnosis", icd=["I10"]))
    events = diff_field_events(before=before, after=after, field_types=_tpl_types())
    assert [e.kind for e in events] == ["confirmed"]
    assert events[0].payload["codes"] == ["I10"]


def test_picking_a_different_code_emits_overridden() -> None:
    before = _content(
        _section(
            "diagnosis",
            meta={
                "proposals": [{"code": "I10", "confidence": 0.9}],
                "confidence": 0.9,
                "source": "extracted",
            },
        )
    )
    after = _content(_section("diagnosis", icd=["I11.0"]))
    events = diff_field_events(before=before, after=after, field_types=_tpl_types())
    assert [e.kind for e in events] == ["overridden"]
    assert events[0].payload["codes"] == ["I11.0"]
    assert events[0].payload["proposed"] == ["I10"]


def test_free_text_override_carries_no_text() -> None:
    """THE PHI LINE: never record what prose a clinician wrote."""
    before = _content(
        _section(
            "complaints",
            text="старий текст",
            meta={"selected": "x", "confidence": 0.9, "source": "extracted"},
        )
    )
    after = _content(
        _section(
            "complaints",
            text="новий текст з приватними даними",
            meta={"selected": "y", "source": "manual"},
        )
    )
    events = diff_field_events(before=before, after=after, field_types=_tpl_types())
    for event in events:
        serialized = repr(event.payload)
        assert "текст" not in serialized, event.payload
        assert "приватними" not in serialized
        assert "selected" not in event.payload, "free_text is not a closed vocabulary"


def test_no_change_emits_nothing() -> None:
    content = _content(_section("smoking_status", meta={"selected": "never", "source": "manual"}))
    assert diff_field_events(before=content, after=content, field_types=_tpl_types()) == []


def test_purely_extracted_write_emits_nothing() -> None:
    """The extractor filling a field is not a clinician action."""
    after = _content(
        _section(
            "smoking_status", meta={"selected": "never", "confidence": 0.9, "source": "extracted"}
        )
    )
    assert diff_field_events(before=None, after=after, field_types=_tpl_types()) == []
