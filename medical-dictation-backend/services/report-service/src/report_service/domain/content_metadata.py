"""Sprint-13: write-path validation of ``field_specific_metadata``.

Every content write (create / draft PUT / amend) validates each
section's metadata dict against the section's template ``field_type``
(via ``report_models.validate_field_metadata``) and — for
choice/multi_choice — checks the ``selected`` value(s) against the
template's option ``value``s. The template is resolved the same way
finalize validation resolves it (``domain.repository.get_template`` by
``content.template_id``), and only when at least one section actually
carries metadata, so pre-S13 autosaves pay no extra query.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from report_models import (
    ChoiceMeta,
    FieldMetadataError,
    MultiChoiceMeta,
    ReportContent,
    parse_field_metadata,
)
from template_models import TemplateDefinition, TemplateSection

from . import repository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MetadataProblem:
    code: str  # 'field_metadata_invalid' | 'choice_value_unknown'
    section_key: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "section_key": self.section_key, "reason": self.reason}


def validate_content_metadata(
    content: ReportContent, template: TemplateDefinition
) -> list[MetadataProblem]:
    """Pure check of every section's metadata against the template."""
    problems: list[MetadataProblem] = []
    tpl_by_id: dict[str, TemplateSection] = {s.id: s for s in template.sections}

    for section in content.sections:
        md: dict[str, Any] = section.field_specific_metadata
        if not md:
            continue
        tpl_section = tpl_by_id.get(section.section_key)
        if tpl_section is None:
            problems.append(
                MetadataProblem(
                    code="field_metadata_invalid",
                    section_key=section.section_key,
                    reason="section is not defined in the report's template",
                )
            )
            continue
        try:
            meta = parse_field_metadata(tpl_section.field_type, md)
        except FieldMetadataError as exc:
            problems.append(
                MetadataProblem(
                    code="field_metadata_invalid",
                    section_key=section.section_key,
                    reason=exc.reason,
                )
            )
            continue

        if isinstance(meta, ChoiceMeta | MultiChoiceMeta):
            allowed = {opt.value for opt in tpl_section.options}
            selected = (meta.selected,) if isinstance(meta, ChoiceMeta) else meta.selected
            for value in selected:
                if value not in allowed:
                    problems.append(
                        MetadataProblem(
                            code="choice_value_unknown",
                            section_key=section.section_key,
                            reason=f"selected value {value!r} is not an option of this section",
                        )
                    )
    return problems


async def check_content_metadata(conn: object, *, content: ReportContent) -> list[MetadataProblem]:
    """Resolve the template and validate; template fetch is skipped when
    no section carries metadata (the pre-S13 fast path)."""
    if not any(s.field_specific_metadata for s in content.sections):
        return []

    try:
        tmpl_row = await repository.get_template(conn, template_id=content.template_id)
    except Exception:
        logger.warning(
            "could not resolve template %s for metadata validation",
            content.template_id,
            exc_info=True,
        )
        tmpl_row = None
    if tmpl_row is None:
        return [
            MetadataProblem(
                code="field_metadata_invalid",
                section_key=s.section_key,
                reason="template not found; metadata cannot be validated",
            )
            for s in content.sections
            if s.field_specific_metadata
        ]
    raw = tmpl_row["schema_jsonb"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    template = TemplateDefinition.model_validate(raw)
    return validate_content_metadata(content, template)


async def template_field_types(conn: object, *, content: ReportContent) -> dict[str, str]:
    """``{section_key: field_type}`` for the content's template.

    Returns ``{}`` — never raises — when the template cannot be
    resolved: the confirm/override audit signal is telemetry, and
    losing it must not fail a clinician's autosave.
    """
    try:
        tmpl_row = await repository.get_template(conn, template_id=content.template_id)
        if tmpl_row is None:
            return {}
        raw = tmpl_row["schema_jsonb"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        template = TemplateDefinition.model_validate(raw)
    except Exception:
        logger.warning(
            "could not resolve template %s for field-audit typing",
            content.template_id,
            exc_info=True,
        )
        return {}
    return {s.id: s.field_type.value for s in template.sections}
