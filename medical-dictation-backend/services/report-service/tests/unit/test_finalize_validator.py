"""Finalize validator coverage."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from report_models import Icd10Code, ReportContent, ReportSection
from report_service.domain.finalize_validator import validate_finalize


def _template(sections):
    return SimpleNamespace(sections=[SimpleNamespace(**s) for s in sections])


def _content(sections=(), icd=None):
    return ReportContent(
        template_id=uuid4(),
        template_schema_version=1,
        sections=[ReportSection(section_key=k, text=t, icd10=ic) for (k, t, ic) in sections],
        icd10_codes=[Icd10Code(code=c) for c in (icd or [])],
    )


def test_missing_required_section_flagged():
    tpl = _template([{"key": "ccx", "required": True, "min_chars": 0, "icd10_required": False}])
    c = _content()
    problems = validate_finalize(content=c, template=tpl)
    assert any(p.code == "missing_required_section" for p in problems)


def test_below_min_chars_flagged():
    tpl = _template([{"key": "ccx", "required": True, "min_chars": 20, "icd10_required": False}])
    c = _content(sections=[("ccx", "short", [])])
    problems = validate_finalize(content=c, template=tpl)
    assert any(p.code == "below_min_chars" for p in problems)


def test_icd10_required_satisfied_at_top_level():
    tpl = _template([{"key": "ccx", "required": False, "min_chars": 0, "icd10_required": True}])
    c = _content(sections=[("ccx", "any", [])], icd=["I21"])
    problems = validate_finalize(content=c, template=tpl)
    assert problems == []


def test_icd10_required_missing_flagged():
    tpl = _template([{"key": "ccx", "required": False, "min_chars": 0, "icd10_required": True}])
    c = _content(sections=[("ccx", "any", [])], icd=[])
    problems = validate_finalize(content=c, template=tpl)
    assert any(p.code == "missing_icd10" for p in problems)


def test_optional_section_empty_not_flagged():
    tpl = _template([{"key": "ccx", "required": False, "min_chars": 0, "icd10_required": False}])
    c = _content()
    assert validate_finalize(content=c, template=tpl) == []
