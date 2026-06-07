"""libs/template_models — Pydantic models for the templates JSONB schema.

The schema is a public contract: sprint 8 reports persist
``template_id`` + ``template_version`` at finalization, sprint 11
encounters reference reports, sprint 13 anamnesis reads field types,
sprint 17 admin emits FHIR Composition from metadata. Changing the
shape requires either a cosmetic edit (in-place, schema_version bump)
or a structural edit (new row, parent_template_id set) — see
:func:`classify_edit`.
"""

from __future__ import annotations

from .schema import (
    ASR_PROMPT_MAX_TOKENS,
    FIELD_TYPES,
    EditKind,
    FieldType,
    TemplateDefinition,
    TemplateMetadata,
    TemplateSection,
    classify_edit,
)

__all__ = [
    "ASR_PROMPT_MAX_TOKENS",
    "EditKind",
    "FIELD_TYPES",
    "FieldType",
    "TemplateDefinition",
    "TemplateMetadata",
    "TemplateSection",
    "classify_edit",
]
