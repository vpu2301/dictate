"""Pre-transition finalize validation.

Ensures every section flagged ``required`` in the template has content
≥ ``min_chars``, and ICD-10 is present where ``icd10_required`` is
true.
"""

from __future__ import annotations

from dataclasses import dataclass

from report_models import ReportContent
from template_models import TemplateDefinition


@dataclass(slots=True)
class FinalizeProblem:
    field: str
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "detail": self.detail}


def validate_finalize(
    *, content: ReportContent, template: TemplateDefinition
) -> list[FinalizeProblem]:
    problems: list[FinalizeProblem] = []
    by_key = {s.section_key: s for s in content.sections}
    for tpl_section in template.sections:
        body = by_key.get(tpl_section.key)
        required = bool(getattr(tpl_section, "required", False))
        min_chars = int(getattr(tpl_section, "min_chars", 0) or 0)

        if required and (body is None or len(body.text.strip()) == 0):
            problems.append(
                FinalizeProblem(
                    field=f"sections.{tpl_section.key}.text",
                    code="missing_required_section",
                    detail=f"section {tpl_section.key!r} is required",
                )
            )
            continue
        if body is not None and min_chars > 0 and len(body.text.strip()) < min_chars:
            problems.append(
                FinalizeProblem(
                    field=f"sections.{tpl_section.key}.text",
                    code="below_min_chars",
                    detail=f"section {tpl_section.key!r} needs at least {min_chars} chars",
                )
            )
        if getattr(tpl_section, "icd10_required", False):
            has_icd = bool(body and body.icd10) or bool(content.icd10_codes)
            if not has_icd:
                problems.append(
                    FinalizeProblem(
                        field=f"sections.{tpl_section.key}.icd10",
                        code="missing_icd10",
                        detail=f"section {tpl_section.key!r} requires at least one ICD-10 code",
                    )
                )
    return problems
