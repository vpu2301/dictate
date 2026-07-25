"""Sprint-08 ``report_models``.

Strict Pydantic shapes for ``reports`` + ``report_versions``. Used by
report-service routers and by the chain-reconciler integrity check.

The canonical projection that sprint-09 will sign is
``ReportContent.canonical_bytes()`` — stable, deterministic, RFC-8785
JSON canonicalisation over the fixed schema.
"""

from report_models.content import (
    Icd10Code,
    ReportAmendmentType,
    ReportContent,
    ReportSection,
    ReportStatus,
    canonical_content_bytes,
    rendered_text_from_content,
)
from report_models.diff import (
    DiffResponse,
    DiffSectionEntry,
    DiffSegment,
    MetadataDiff,
)
from report_models.field_metadata import (
    META_MODEL_BY_FIELD_TYPE,
    ChoiceMeta,
    DateMeta,
    DiagnosisMeta,
    DiagnosisProposal,
    FieldMeta,
    FieldMetadataError,
    MetadataSource,
    MultiChoiceMeta,
    NumericMeta,
    parse_field_metadata,
    validate_field_metadata,
)
from report_models.read_purpose import ReadPurpose

__all__ = [
    "META_MODEL_BY_FIELD_TYPE",
    "ChoiceMeta",
    "DateMeta",
    "DiagnosisMeta",
    "DiagnosisProposal",
    "DiffResponse",
    "DiffSectionEntry",
    "DiffSegment",
    "FieldMeta",
    "FieldMetadataError",
    "Icd10Code",
    "MetadataDiff",
    "MetadataSource",
    "MultiChoiceMeta",
    "NumericMeta",
    "ReadPurpose",
    "ReportAmendmentType",
    "ReportContent",
    "ReportSection",
    "ReportStatus",
    "canonical_content_bytes",
    "parse_field_metadata",
    "rendered_text_from_content",
    "validate_field_metadata",
]
