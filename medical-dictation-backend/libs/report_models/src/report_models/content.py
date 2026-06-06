"""ReportContent — the JSON shape stored in ``report_versions.content_jsonb``.

Sprint-08 design notes:

- ``extra='forbid'`` everywhere. The shape is the contract that
  sprint-09 signing will commit to; unknown keys silently dropped
  would invalidate signatures.
- ``ReportSection.field_specific_metadata`` is an open dict — this is
  the documented escape hatch for sprint-13 (anamnesis) and sprint-15
  (note review) to attach typed metadata without re-versioning every
  template. The allowed keys per ``field_type`` are documented in
  ``docs/architecture/reports.md``.
- ``canonical_content_bytes`` produces the RFC-8785 JCS serialisation
  used as input to sprint-09 signing. The model JSON dump is sorted
  by Pydantic; we re-sort defensively here.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReportStatus(StrEnum):
    DRAFT = "draft"
    FINALIZED = "finalized"
    SIGNED = "signed"
    AMENDED = "amended"
    CANCELLED = "cancelled"


class ReportAmendmentType(StrEnum):
    CORRECTION = "correction"
    ADDITION = "addition"
    CLARIFICATION = "clarification"


class Icd10Code(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=3, max_length=8, pattern=r"^[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$")
    display: str | None = Field(default=None, max_length=200)

    @field_validator("code", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_key: str = Field(min_length=1, max_length=64)
    text: str = ""
    transcript_segment_ids: list[UUID] = Field(default_factory=list)
    icd10: list[Icd10Code] = Field(default_factory=list)
    field_specific_metadata: dict[str, Any] = Field(default_factory=dict)


class ReportContent(BaseModel):
    """The full content_jsonb body for one version."""

    model_config = ConfigDict(extra="forbid")

    template_id: UUID
    template_schema_version: int = Field(ge=1)
    title: str = ""
    encounter_date: str | None = None  # ISO-8601 date; not datetime
    sections: list[ReportSection] = Field(default_factory=list)
    icd10_codes: list[Icd10Code] = Field(default_factory=list)

    @field_validator("sections")
    @classmethod
    def _unique_section_keys(cls, v: list[ReportSection]) -> list[ReportSection]:
        keys = [s.section_key for s in v]
        if len(keys) != len(set(keys)):
            raise ValueError("section_key values must be unique within a report")
        return v


def canonical_content_bytes(content: ReportContent) -> bytes:
    """RFC-8785 canonical JSON of the content. Used by sprint-09 signing.

    Stable across Python versions because:
    - keys sorted alphabetically;
    - no whitespace;
    - UTF-8 with no non-ASCII escape;
    - no Pydantic round-trip drift (model_dump → json with sort_keys).
    """
    obj = content.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def rendered_text_from_content(content: ReportContent) -> str:
    """Plain-text projection used for ``rendered_text`` + FTS.

    Concatenates section title + body, separated by double newlines.
    Order follows ``content.sections`` (template-order, by convention).
    """
    parts: list[str] = []
    if content.title:
        parts.append(content.title)
    for s in content.sections:
        header = s.section_key
        body = s.text.strip()
        if body:
            parts.append(f"{header}\n{body}")
    return "\n\n".join(parts)
