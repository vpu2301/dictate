"""Diff response models. Day-5 endpoint shape."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DiffSectionKind = Literal["added", "removed", "modified", "unchanged"]


class DiffSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["equal", "insert", "delete", "replace"]
    text_from: str = ""
    text_to: str = ""


class DiffSectionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_key: str
    kind: DiffSectionKind
    text_from: str = ""
    text_to: str = ""
    segments: list[DiffSegment] = Field(default_factory=list)


class MetadataDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_changed: bool = False
    title_from: str | None = None
    title_to: str | None = None
    icd10_added: list[str] = Field(default_factory=list)
    icd10_removed: list[str] = Field(default_factory=list)
    encounter_date_changed: bool = False
    encounter_date_from: str | None = None
    encounter_date_to: str | None = None


class DiffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    from_version_id: str
    from_version_number: int
    to_version_id: str
    to_version_number: int
    sections: list[DiffSectionEntry] = Field(default_factory=list)
    metadata: MetadataDiff = Field(default_factory=MetadataDiff)
    cached: bool = False
