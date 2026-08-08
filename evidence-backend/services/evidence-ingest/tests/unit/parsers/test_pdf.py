from __future__ import annotations

from pathlib import Path

import pytest
from evidence_ingest.domain.parse_types import ParserError
from evidence_ingest.domain.parsers import pdf


def test_who_guideline(fixtures_dir: Path) -> None:
    doc = pdf.parse((fixtures_dir / "who_guideline.pdf").read_bytes())
    assert doc.title == "WHO Guideline on Diabetes Care"
    assert doc.language == "en"
    assert doc.metadata["doi"] == "10.1234/who.2024.001"
    assert doc.metadata["published"] == "2024-03-15"
    assert [(s.path, s.level) for s in doc.sections] == [
        ("", 0),
        ("1 Scope", 1),
        ("2 Recommendations", 1),
        ("2 Recommendations > 2.1 Monitoring", 2),
    ]
    assert "screening and management" in doc.sections[1].text


def test_moz_protocol_transliterated(fixtures_dir: Path) -> None:
    doc = pdf.parse((fixtures_dir / "moz_protocol.pdf").read_bytes())
    assert [s.path for s in doc.sections] == ["", "ROZDIL 1", "DODATOK A"]
    assert doc.metadata["published"] == "2023-01-21"
    # Transliterated fixture: no Cyrillic, so no 'uk' detection and no moz_order.
    assert doc.language in ("en", None)


def test_corrupt_pdf_raises(fixtures_dir: Path) -> None:
    with pytest.raises(ParserError):
        pdf.parse((fixtures_dir / "corrupt.pdf").read_bytes())


def test_empty_bytes_raise() -> None:
    with pytest.raises(ParserError):
        pdf.parse(b"")
