from __future__ import annotations

from pathlib import Path

import pytest
from evidence_ingest.domain.parse_types import ParserError, full_text
from evidence_ingest.domain.parsers import html_guideline


def test_nice_guideline(fixtures_dir: Path) -> None:
    doc = html_guideline.parse((fixtures_dir / "nice_guideline.html").read_bytes())
    assert doc.title == "Type 2 diabetes in adults: management | Guidance"
    assert doc.language == "en"
    assert doc.metadata["doi"] == "10.7777/nice.ng28"
    assert doc.metadata["published"] == "2024-06-19"
    assert doc.metadata["organization"] == "NICE"
    h1 = "Type 2 diabetes in adults: management"
    assert [(s.path, s.level) for s in doc.sections] == [
        (h1, 1),
        (f"{h1} > Recommendations", 2),
        (f"{h1} > Recommendations > Drug treatment", 3),
    ]
    assert "Metformin\t500 mg twice daily" in doc.sections[2].text
    assert "NAVJUNK" not in full_text(doc)


def test_no_headings_yields_single_section() -> None:
    doc = html_guideline.parse(b"<html><body><p>Plain body text only.</p></body></html>")
    assert [(s.path, s.level) for s in doc.sections] == [("", 0)]
    assert doc.sections[0].text == "Plain body text only."


def test_empty_html_raises() -> None:
    with pytest.raises(ParserError, match="no_extractable_text"):
        html_guideline.parse(b"<html><body><script>var x = 1;</script></body></html>")
