"""Pre-transition finalize validation.

Ensures every section flagged ``required`` in the template has content
≥ ``min_chars``, and ICD-10 is present where ``icd10_required`` is
true.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from report_models import ReportContent
from template_models import TemplateDefinition

# Normalised, contract-facing reasons keyed by the legacy ``code``.
# Anything unmapped falls through to the code itself.
_REASON_BY_CODE: Final[dict[str, str]] = {
    "missing_required_section": "required_empty",
    "below_min_chars": "below_min_chars",
    "missing_icd10": "missing_icd10",
}


@dataclass(slots=True)
class FinalizeProblem:
    field: str
    code: str
    detail: str
    section_key: str | None = None

    @property
    def reason(self) -> str:
        return _REASON_BY_CODE.get(self.code, self.code)

    def as_dict(self) -> dict[str, str | None]:
        # Legacy keys (field/code/detail) retained for backward compat;
        # section_key + reason added for the aligned finalize contract.
        return {
            "field": self.field,
            "code": self.code,
            "detail": self.detail,
            "section_key": self.section_key,
            "reason": self.reason,
        }


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
                    section_key=tpl_section.key,
                )
            )
            continue
        if body is not None and min_chars > 0 and len(body.text.strip()) < min_chars:
            problems.append(
                FinalizeProblem(
                    field=f"sections.{tpl_section.key}.text",
                    code="below_min_chars",
                    detail=f"section {tpl_section.key!r} needs at least {min_chars} chars",
                    section_key=tpl_section.key,
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
                        section_key=tpl_section.key,
                    )
                )
    return problems
