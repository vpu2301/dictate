"""Canonical-JSON-for-signing byte-stability tests."""

from __future__ import annotations

import json
from uuid import uuid4

from medical_kep import canonicalize_report
from medical_kep.canonicalize import CanonicalReportInput


def _input(**overrides):
    base = {
        "tenant_id": str(uuid4()),
        "tenant_legal_name": "МедЦентр «Тест»",
        "report_id": str(uuid4()),
        "report_code": "REP-2026-00001",
        "report_version_id": str(uuid4()),
        "report_version_number": 1,
        "title": "Кардіологічна консультація",
        "encounter_date": "2026-05-10",
        "primary_author_full_name": "Лікар Тестовий",
        "primary_author_id": str(uuid4()),
        "primary_author_role": "clinician",
        "co_author_names": ["Аркадій"],
        "patient_id": str(uuid4()),
        "patient_full_name_redacted": "І. І. І.",
        "icd10_codes": ["I21", "I50.0"],
        "sections": [
            {
                "section_key": "chief_complaint",
                "text": "біль у грудях",
                "transcript_segment_ids": [],
            },
        ],
        "template_id": str(uuid4()),
        "template_schema_version": 1,
        "finalized_at": "2026-05-10T12:34:56Z",
        "signed_at_intent": "2026-05-10T12:35:00Z",
    }
    base.update(overrides)
    return CanonicalReportInput(**base)


def test_canonical_bytes_deterministic_across_calls():
    inp = _input()
    a, ha = canonicalize_report(inp)
    b, hb = canonicalize_report(inp)
    assert a == b
    assert ha == hb


def test_changing_text_changes_hash():
    a, ha = canonicalize_report(_input())
    b, hb = canonicalize_report(
        _input(sections=[{"section_key": "x", "text": "y", "transcript_segment_ids": []}])
    )
    assert a != b
    assert ha != hb


def test_icd10_set_normalised():
    a, _ = canonicalize_report(_input(icd10_codes=["I21", "I50.0", "I21"]))
    obj = json.loads(a)
    assert obj["report"]["icd10_codes"] == ["I21", "I50.0"]


def test_canonical_version_present():
    a, _ = canonicalize_report(_input())
    obj = json.loads(a)
    assert obj["canonical_version"] == "1.0"


def test_no_patient_path():
    a, _ = canonicalize_report(_input(patient_id=None, patient_full_name_redacted=None))
    obj = json.loads(a)
    assert obj["report"]["patient"] is None


def test_jcs_sorted_keys():
    a, _ = canonicalize_report(_input())
    obj_str = a.decode("utf-8")
    # Top-level keys must appear in alphabetical JCS order:
    assert (
        obj_str.index('"canonical_version"')
        < obj_str.index('"lifecycle"')
        < obj_str.index('"report"')
        < obj_str.index('"tenant"')
    )
