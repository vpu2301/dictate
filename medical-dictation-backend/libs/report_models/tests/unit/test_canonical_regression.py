"""Sprint-13 signing regression guard (canonical bytes).

Sprint-09 signatures commit to ``canonical_content_bytes``. The S13
metadata contract must not change a single byte of any pre-S13
content's canonical form — frozen fixtures (committed with the S13 PR,
generated before the lib change) pin that. New-shape content must
canonicalize deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from report_models import ReportContent, canonical_content_bytes

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "canonical_pre_s13"
_CASES = sorted(p.stem.removesuffix(".input") for p in _FIXTURES.glob("*.input.json"))


@pytest.mark.parametrize("case", _CASES)
def test_pre_s13_canonical_bytes_unchanged(case: str) -> None:
    doc = json.loads((_FIXTURES / f"{case}.input.json").read_text("utf-8"))
    expected = (_FIXTURES / f"{case}.canonical.bin").read_bytes()
    got = canonical_content_bytes(ReportContent.model_validate(doc))
    assert got == expected, f"{case}: canonical bytes drifted — old signatures would break"


def test_fixture_corpus_present() -> None:
    assert len(_CASES) == 3, "the frozen pre-S13 corpus must stay committed"


def _new_shape_content() -> ReportContent:
    return ReportContent.model_validate(
        {
            "template_id": "70cd91de-82b0-48e5-81ce-dcc01e0a2297",
            "template_schema_version": 1,
            "title": "Анамнез — первинний прийом",
            "sections": [
                {
                    "section_key": "smoking_status",
                    "text": "пацієнт не палить",
                    "field_specific_metadata": {
                        "selected": "never",
                        "confidence": 0.92,
                        "source": "extracted",
                    },
                },
                {
                    "section_key": "allergies",
                    "text": "алергія на пеніцилін та латекс",
                    "field_specific_metadata": {
                        "selected": ["penicillin", "latex"],
                        "confidence": 0.87,
                        "source": "extracted",
                    },
                },
            ],
        }
    )


def test_new_shape_double_canonicalize_byte_equal() -> None:
    c = _new_shape_content()
    assert canonical_content_bytes(c) == canonical_content_bytes(c)


def test_new_shape_roundtrip_canonical_stable() -> None:
    c = _new_shape_content()
    b = canonical_content_bytes(c)
    reparsed = ReportContent.model_validate(json.loads(b.decode("utf-8")))
    assert canonical_content_bytes(reparsed) == b


def test_metadata_serializes_verbatim_in_canonical_bytes() -> None:
    b = canonical_content_bytes(_new_shape_content())
    obj = json.loads(b.decode("utf-8"))
    assert obj["sections"][0]["field_specific_metadata"] == {
        "selected": "never",
        "confidence": 0.92,
        "source": "extracted",
    }
