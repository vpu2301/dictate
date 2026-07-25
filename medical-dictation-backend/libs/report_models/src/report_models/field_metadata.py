"""Sprint-13: the typed contract for ``ReportSection.field_specific_metadata``.

``field_specific_metadata`` stays ``dict[str, Any]`` on the model —
canonical bytes (sprint-09 signatures) serialize the dict as-is, so the
*storage* shape must never depend on this module. Discipline is enforced
at the WRITE path instead: report-service validates every non-empty dict
against the section's template ``field_type`` via
:func:`validate_field_metadata`, and the nlp extractor (sprint-13 step
04) constructs metadata via these models so it cannot emit invalid
shapes.

The normative key registry lives in ``docs/architecture/reports.md``.
Contract rules:

- An empty dict is always valid (every pre-S13 report).
- ``source`` is required whenever any other key is present:
  ``extracted`` values are proposals the FE renders as such; ``manual``
  values are clinician-confirmed. Nothing ever auto-promotes
  extracted → manual; that is exclusively an explicit user action.
- ``confidence`` is required for ``extracted`` entries (the extractor
  always knows it) and must be OMITTED for ``manual`` entries (a
  clinician confirmation is not a probability — documented choice,
  sprint-13 step 02).
- ``structured_diagnosis`` metadata carries ``proposals`` — the
  extractor's staging area. Confirmed codes live in ``section.icd10``
  (single authority; mirroring confirmed codes here would invite
  dual-write drift).

``report_models`` is an import-linter leaf, so ``field_type`` is passed
as ``str`` — ``template_models.FieldType`` is a ``StrEnum`` and passes
through unchanged.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class MetadataSource(StrEnum):
    EXTRACTED = "extracted"
    MANUAL = "manual"


class FieldMetadataError(ValueError):
    """Raised when a metadata dict violates the field-type contract."""

    def __init__(self, field_type: str, reason: str) -> None:
        self.field_type = field_type
        self.reason = reason
        super().__init__(f"field_specific_metadata invalid for {field_type!r}: {reason}")


class _StrictMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: MetadataSource
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _confidence_matches_source(self) -> _StrictMeta:
        if self.source is MetadataSource.EXTRACTED and self.confidence is None:
            raise ValueError("extracted metadata requires confidence")
        if self.source is MetadataSource.MANUAL and self.confidence is not None:
            raise ValueError("manual metadata must omit confidence")
        return self


class ChoiceMeta(_StrictMeta):
    """``choice`` — a single selected option ``value``."""

    selected: str = Field(min_length=1, max_length=64)


class MultiChoiceMeta(_StrictMeta):
    """``multi_choice`` — the selected option ``value``s (≥ 1; an empty
    selection is represented by an empty metadata dict, not an empty
    list)."""

    selected: tuple[str, ...] = Field(min_length=1, max_length=50)

    @field_validator("selected")
    @classmethod
    def _unique(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError("selected values must be unique")
        return v


class DiagnosisProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Same shape rule as report_models.Icd10Code (kept in lock-step by test).
    code: str = Field(min_length=3, max_length=8, pattern=r"^[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$")
    display: str | None = Field(default=None, max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("code", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v


class DiagnosisMeta(_StrictMeta):
    """``structured_diagnosis`` — extractor PROPOSALS only.

    Confirmed codes live in ``section.icd10`` (the single authority).
    Confirming a proposal moves its code there and may clear
    ``proposals``; a proposal is never auto-selected.
    """

    proposals: tuple[DiagnosisProposal, ...] = Field(min_length=1, max_length=20)


class NumericMeta(_StrictMeta):
    """``numeric_with_unit`` — a bound value + unit."""

    value: float
    unit: str = Field(min_length=1, max_length=32)


class DateMeta(_StrictMeta):
    """``date`` / ``date_with_note`` — an ISO calendar date."""

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("date")
    @classmethod
    def _real_date(cls, v: str) -> str:
        try:
            _dt.date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(f"date {v!r} is not a real calendar date") from exc
        return v


FieldMeta = ChoiceMeta | MultiChoiceMeta | DiagnosisMeta | NumericMeta | DateMeta

# The registry: field types absent here (free_text) accept no metadata.
# Sprint-15 (note review) extends this table — never bypasses it.
META_MODEL_BY_FIELD_TYPE: Final[Mapping[str, type[FieldMeta]]] = {
    "choice": ChoiceMeta,
    "multi_choice": MultiChoiceMeta,
    "structured_diagnosis": DiagnosisMeta,
    "numeric_with_unit": NumericMeta,
    "date": DateMeta,
    "date_with_note": DateMeta,
}


def parse_field_metadata(field_type: str, metadata: Mapping[str, Any]) -> FieldMeta | None:
    """Parse a section's metadata dict into its typed model.

    Returns ``None`` for an empty dict (always valid — every pre-S13
    report). Raises :class:`FieldMetadataError` for unknown keys, wrong
    shapes, or metadata on a field type that accepts none.
    """
    if not metadata:
        return None
    model = META_MODEL_BY_FIELD_TYPE.get(field_type)
    if model is None:
        raise FieldMetadataError(field_type, "this field_type accepts no metadata keys")
    try:
        return model.model_validate(dict(metadata))
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise FieldMetadataError(field_type, f"{loc}: {first['msg']}") from exc


def validate_field_metadata(field_type: str, metadata: Mapping[str, Any]) -> None:
    """Raise :class:`FieldMetadataError` unless ``metadata`` is valid.

    By construction accepts exactly what :func:`parse_field_metadata`
    parses (it *is* a parse-and-discard; a property test pins this).
    """
    parse_field_metadata(field_type, metadata)
