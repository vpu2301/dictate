"""Day-5 diff engine.

Produces a structured diff between two ``ReportContent`` snapshots:

- Per-section classification (added/removed/modified/unchanged).
- Char-level segments via ``difflib.SequenceMatcher`` for modified
  sections.
- Metadata diff: title, ICD-10 set-difference, encounter_date.

Deterministic — same inputs → byte-equal output. The diff endpoint
caches by ``(from_id, to_id)`` and content versions are immutable,
so cache hits are free of staleness concerns (ADR-0020 corollary).
"""

from __future__ import annotations

import difflib
from typing import Final

from report_models import (
    DiffResponse,
    DiffSectionEntry,
    DiffSegment,
    MetadataDiff,
    ReportContent,
)

_SECTION_KIND_ADDED: Final = "added"
_SECTION_KIND_REMOVED: Final = "removed"
_SECTION_KIND_MODIFIED: Final = "modified"
_SECTION_KIND_UNCHANGED: Final = "unchanged"


def _segments(text_from: str, text_to: str) -> list[DiffSegment]:
    """SequenceMatcher-based char-level diff."""
    out: list[DiffSegment] = []
    matcher = difflib.SequenceMatcher(a=text_from, b=text_to, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        out.append(
            DiffSegment(
                op=op,
                text_from=text_from[i1:i2],
                text_to=text_to[j1:j2],
            )
        )
    return out


def compute_diff(
    *,
    report_id: str,
    from_version_id: str,
    from_version_number: int,
    from_content: ReportContent,
    to_version_id: str,
    to_version_number: int,
    to_content: ReportContent,
) -> DiffResponse:
    from_by_key = {s.section_key: s for s in from_content.sections}
    to_by_key = {s.section_key: s for s in to_content.sections}
    all_keys = list(from_by_key) + [k for k in to_by_key if k not in from_by_key]

    sections: list[DiffSectionEntry] = []
    for key in all_keys:
        f = from_by_key.get(key)
        t = to_by_key.get(key)
        if f is None and t is not None:
            sections.append(
                DiffSectionEntry(
                    section_key=key,
                    kind=_SECTION_KIND_ADDED,
                    text_from="",
                    text_to=t.text,
                    segments=_segments("", t.text),
                )
            )
        elif f is not None and t is None:
            sections.append(
                DiffSectionEntry(
                    section_key=key,
                    kind=_SECTION_KIND_REMOVED,
                    text_from=f.text,
                    text_to="",
                    segments=_segments(f.text, ""),
                )
            )
        else:
            assert f is not None and t is not None
            if f.text == t.text:
                sections.append(
                    DiffSectionEntry(
                        section_key=key,
                        kind=_SECTION_KIND_UNCHANGED,
                        text_from=f.text,
                        text_to=t.text,
                        segments=[],
                    )
                )
            else:
                sections.append(
                    DiffSectionEntry(
                        section_key=key,
                        kind=_SECTION_KIND_MODIFIED,
                        text_from=f.text,
                        text_to=t.text,
                        segments=_segments(f.text, t.text),
                    )
                )

    metadata = _metadata_diff(from_content, to_content)
    return DiffResponse(
        report_id=report_id,
        from_version_id=from_version_id,
        from_version_number=from_version_number,
        to_version_id=to_version_id,
        to_version_number=to_version_number,
        sections=sections,
        metadata=metadata,
    )


def _metadata_diff(a: ReportContent, b: ReportContent) -> MetadataDiff:
    from_icd = {c.code for c in a.icd10_codes} | {c.code for s in a.sections for c in s.icd10}
    to_icd = {c.code for c in b.icd10_codes} | {c.code for s in b.sections for c in s.icd10}
    return MetadataDiff(
        title_changed=(a.title != b.title),
        title_from=a.title if a.title != b.title else None,
        title_to=b.title if a.title != b.title else None,
        icd10_added=sorted(to_icd - from_icd),
        icd10_removed=sorted(from_icd - to_icd),
        encounter_date_changed=(a.encounter_date != b.encounter_date),
        encounter_date_from=a.encounter_date if a.encounter_date != b.encounter_date else None,
        encounter_date_to=b.encounter_date if a.encounter_date != b.encounter_date else None,
    )


def section_diff_summary(diff: DiffResponse) -> dict[str, list[str]]:
    """Compact representation suitable for ``report_versions.diff_jsonb``."""
    return {
        "added": [s.section_key for s in diff.sections if s.kind == "added"],
        "removed": [s.section_key for s in diff.sections if s.kind == "removed"],
        "modified": [s.section_key for s in diff.sections if s.kind == "modified"],
    }
