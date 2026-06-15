"""Templates JSONB schema (sprint 06).

Public contract:
- ``extra="forbid"`` on every model. Sprint-7 evals + sprint-8 reports
  rely on schema stability; an unknown field is either a typo (catch
  early) or a real new feature (forces a Pydantic model bump + ADR).
- ``ASR_PROMPT_MAX_TOKENS = 224`` matches Whisper's ``initial_prompt``
  context window. Authoring guidance lives in
  ``docs/clinical-content/template-authoring.md``.
- The five ``FieldType`` values cover the sprint-06 surface. Sprint 13
  (anamnesis) adds typed fields like ``CHOICE`` and ``MULTI_CHOICE``;
  those go through an additive model bump.

``classify_edit`` is the cosmetic-vs-structural decision rule from
ADR-0016. Cosmetic edits UPDATE in place + bump ``schema_version``;
structural edits INSERT a new row with ``parent_template_id`` set
(``schema_version`` reset to 1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Whisper's ``initial_prompt`` accepts up to 224 BPE tokens. We accept a
# slightly conservative limit because token counting is per-tokenizer
# and we don't want to embed tiktoken in the validation hot path. The
# authorship-time validator (scripts/validate-templates.py) uses
# tiktoken for the exact count; this constant gates runtime length.
ASR_PROMPT_MAX_TOKENS: Final = 224
# Approximate token-to-character ratio for medical text: 4 chars/token
# is a safe upper bound across UK + EN tokenizers.
_APPROX_CHARS_PER_TOKEN: Final = 4
_ASR_PROMPT_MAX_CHARS_APPROX: Final = ASR_PROMPT_MAX_TOKENS * _APPROX_CHARS_PER_TOKEN


class FieldType(StrEnum):
    """Sprint-06 ships these five. Sprint 13 will extend."""

    FREE_TEXT = "free_text"
    STRUCTURED_DIAGNOSIS = "structured_diagnosis"
    DATE = "date"
    DATE_WITH_NOTE = "date_with_note"
    NUMERIC_WITH_UNIT = "numeric_with_unit"


FIELD_TYPES: Final = frozenset(ft.value for ft in FieldType)

_SLUG_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")


class _Strict(BaseModel):
    """Base for every template-domain model. Strict by design."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class TemplateSection(_Strict):
    """One section of a template. Each section is dictated separately
    in sprint-06 section-aware mode."""

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    voice_aliases: tuple[str, ...] = Field(default_factory=tuple)
    required: bool = True
    field_type: FieldType = FieldType.FREE_TEXT
    asr_prompt: str = Field(min_length=1, max_length=_ASR_PROMPT_MAX_CHARS_APPROX)
    min_chars: int = Field(default=0, ge=0, le=10_000)
    order: int = Field(default=0, ge=0)
    default_content: str = Field(default="", max_length=4_000)

    @field_validator("id")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                f"section.id {v!r} must be a URL-safe slug "
                "(lower-case, digits, underscores; starts with letter)"
            )
        return v

    @field_validator("voice_aliases")
    @classmethod
    def _normalize_aliases(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # Lower-case for matching; strip whitespace; reject empties.
        out: list[str] = []
        for alias in v:
            cleaned = alias.strip().lower()
            if not cleaned:
                raise ValueError("voice_aliases must not contain empty strings")
            out.append(cleaned)
        # Deduplicate but preserve order.
        seen: set[str] = set()
        deduped: list[str] = []
        for alias in out:
            if alias not in seen:
                seen.add(alias)
                deduped.append(alias)
        return tuple(deduped)


class TemplateMetadata(_Strict):
    """Optional metadata. Sprint-17 FHIR / Composition emission reads these."""

    moh_order_ref: str | None = Field(default=None, max_length=64)
    billing_code: str | None = Field(default=None, max_length=32)
    fhir_template: str | None = Field(default=None, max_length=128)
    notes: str = Field(default="", max_length=2_000)


class TemplateDefinition(_Strict):
    """Full template definition. Stored as ``templates.schema_jsonb``.

    The top-level ``code`` + ``language`` pair is the human-readable
    identifier; the row's UUID is the system identifier. Sprint 8
    reports persist the UUID + ``schema_version`` at finalization.
    """

    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    language: str = Field(pattern="^(uk|en)$")
    specialty: str = Field(min_length=1, max_length=64)
    schema_version: int = Field(default=1, ge=1)
    sections: tuple[TemplateSection, ...] = Field(min_length=1, max_length=32)
    metadata: TemplateMetadata = Field(default_factory=TemplateMetadata)

    @field_validator("code")
    @classmethod
    def _validate_code_slug(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9_]*$", v):
            raise ValueError(f"template.code {v!r} must be a URL-safe slug")
        return v

    @model_validator(mode="after")
    def _validate_aliases_unique(self) -> TemplateDefinition:
        """Voice aliases must be unique across the template's sections.

        Two sections claiming alias "діагноз" would make the section
        command ambiguous; the matcher would pick the first and
        clinicians would learn it doesn't work.
        """
        seen: dict[str, str] = {}
        for section in self.sections:
            for alias in section.voice_aliases:
                if alias in seen:
                    raise ValueError(
                        f"voice_alias {alias!r} duplicated across sections "
                        f"{seen[alias]!r} and {section.id!r}"
                    )
                seen[alias] = section.id
        return self

    @model_validator(mode="after")
    def _validate_section_ids_unique(self) -> TemplateDefinition:
        seen: set[str] = set()
        for section in self.sections:
            if section.id in seen:
                raise ValueError(f"section.id {section.id!r} duplicated")
            seen.add(section.id)
        return self


# ── Edit classification (ADR-0016) ───────────────────────────────────


class EditKind(StrEnum):
    """Whether the edit can be applied in place or requires a new row."""

    COSMETIC = "cosmetic"
    STRUCTURAL = "structural"
    NO_CHANGE = "no_change"


@dataclass(frozen=True, slots=True)
class EditClassification:
    kind: EditKind
    reasons: tuple[str, ...]


def classify_edit(old: TemplateDefinition, new: TemplateDefinition) -> EditClassification:
    """Decide whether ``new`` replaces ``old`` in place or as a new version.

    **Structural** triggers (any of):
    - a section was added or removed,
    - a section's ``id`` changed,
    - a section's ``field_type`` changed,
    - a section's ``required`` flag flipped,
    - a section's ``min_chars`` increased (loosening is cosmetic).

    Everything else is cosmetic (name/aliases/prompt/order/default_content/metadata).
    A no-change edit is classified as :class:`EditKind.NO_CHANGE` so the
    caller can short-circuit.
    """
    reasons: list[str] = []

    if old.code != new.code:
        reasons.append(f"code changed: {old.code} → {new.code}")
    if old.language != new.language:
        reasons.append(f"language changed: {old.language} → {new.language}")

    old_ids = {s.id for s in old.sections}
    new_ids = {s.id for s in new.sections}
    if old_ids != new_ids:
        added = new_ids - old_ids
        removed = old_ids - new_ids
        if added:
            reasons.append(f"sections added: {sorted(added)}")
        if removed:
            reasons.append(f"sections removed: {sorted(removed)}")

    old_by_id = {s.id: s for s in old.sections}
    new_by_id = {s.id: s for s in new.sections}
    for sid in old_ids & new_ids:
        a = old_by_id[sid]
        b = new_by_id[sid]
        if a.field_type != b.field_type:
            reasons.append(f"section {sid!r}: field_type changed {a.field_type} → {b.field_type}")
        if a.required != b.required:
            reasons.append(f"section {sid!r}: required flipped {a.required} → {b.required}")
        if b.min_chars > a.min_chars:
            reasons.append(f"section {sid!r}: min_chars increased {a.min_chars} → {b.min_chars}")

    if reasons:
        return EditClassification(kind=EditKind.STRUCTURAL, reasons=tuple(reasons))

    # Detect any change at all → cosmetic; else no_change.
    if old.model_dump() == new.model_dump():
        return EditClassification(kind=EditKind.NO_CHANGE, reasons=())
    return EditClassification(kind=EditKind.COSMETIC, reasons=())
