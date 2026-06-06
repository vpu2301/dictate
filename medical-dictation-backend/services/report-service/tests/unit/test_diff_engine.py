"""Unit tests for the diff engine."""

from __future__ import annotations

from uuid import uuid4

import pytest

from report_models import Icd10Code, ReportContent, ReportSection

from report_service.domain.diff_engine import compute_diff, section_diff_summary


def _make(sections, *, title="t", icd=None) -> ReportContent:
    return ReportContent(
        template_id=uuid4(),
        template_schema_version=1,
        title=title,
        sections=[ReportSection(section_key=k, text=t) for k, t in sections],
        icd10_codes=[Icd10Code(code=c) for c in (icd or [])],
    )


def test_unchanged_when_identical():
    a = _make([("x", "hello world")])
    diff = compute_diff(
        report_id="r",
        from_version_id="a",
        from_version_number=1,
        from_content=a,
        to_version_id="b",
        to_version_number=2,
        to_content=a,
    )
    assert all(s.kind == "unchanged" for s in diff.sections)


def test_modified_section_produces_segments():
    a = _make([("x", "the quick brown fox")])
    b = _make([("x", "the quick red fox")])
    diff = compute_diff(
        report_id="r",
        from_version_id="a", from_version_number=1, from_content=a,
        to_version_id="b", to_version_number=2, to_content=b,
    )
    assert diff.sections[0].kind == "modified"
    assert any(seg.op == "replace" for seg in diff.sections[0].segments)


def test_added_and_removed_sections():
    a = _make([("x", "a"), ("y", "b")])
    b = _make([("x", "a"), ("z", "c")])
    diff = compute_diff(
        report_id="r",
        from_version_id="a", from_version_number=1, from_content=a,
        to_version_id="b", to_version_number=2, to_content=b,
    )
    by_key = {s.section_key: s for s in diff.sections}
    assert by_key["y"].kind == "removed"
    assert by_key["z"].kind == "added"
    assert by_key["x"].kind == "unchanged"


def test_metadata_diff_icd10_added_removed():
    a = _make([("x", "")], icd=["I21", "I50.0"])
    b = _make([("x", "")], icd=["I21", "E11.9"])
    diff = compute_diff(
        report_id="r",
        from_version_id="a", from_version_number=1, from_content=a,
        to_version_id="b", to_version_number=2, to_content=b,
    )
    assert "E11.9" in diff.metadata.icd10_added
    assert "I50.0" in diff.metadata.icd10_removed
    assert "I21" not in diff.metadata.icd10_added


def test_title_changed_flag():
    a = _make([("x", "a")], title="old")
    b = _make([("x", "a")], title="new")
    diff = compute_diff(
        report_id="r",
        from_version_id="a", from_version_number=1, from_content=a,
        to_version_id="b", to_version_number=2, to_content=b,
    )
    assert diff.metadata.title_changed
    assert diff.metadata.title_from == "old"
    assert diff.metadata.title_to == "new"


def test_section_diff_summary_lists_section_keys():
    a = _make([("x", "a"), ("y", "b")])
    b = _make([("x", "a-edit"), ("z", "c")])
    diff = compute_diff(
        report_id="r",
        from_version_id="a", from_version_number=1, from_content=a,
        to_version_id="b", to_version_number=2, to_content=b,
    )
    s = section_diff_summary(diff)
    assert s["modified"] == ["x"]
    assert s["removed"] == ["y"]
    assert s["added"] == ["z"]
