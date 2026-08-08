from __future__ import annotations

import re
from pathlib import Path

import pytest
from evidence_ingest.domain.parse_types import ParserError, full_text
from evidence_ingest.domain.parsers import pmc_xml


def test_jats_article(fixtures_dir: Path) -> None:
    doc = pmc_xml.parse((fixtures_dir / "pmc_article.xml").read_bytes())
    assert doc.title == "Hypertension management in primary care"
    assert doc.language == "en"
    assert doc.metadata["doi"] == "10.5555/pmc.2023.42"
    assert doc.metadata["pmid"] == "36000001"
    assert doc.metadata["pmcid"] == "PMC9900001"
    assert doc.metadata["published"] == "2023-07-04"
    assert [(s.path, s.level) for s in doc.sections] == [
        ("Abstract", 1),
        ("Methods", 1),
        ("Methods > Search strategy", 2),
        ("Results", 1),
    ]
    assert "Drug\tDose" in doc.sections[3].text
    assert "Amlodipine\t5 mg" in doc.sections[3].text


def test_medline_abstract(fixtures_dir: Path) -> None:
    doc = pmc_xml.parse((fixtures_dir / "medline_abstract.xml").read_bytes())
    assert doc.title == "Statin therapy for primary prevention"
    assert doc.metadata["pmid"] == "36000002"
    assert [(s.path, s.level) for s in doc.sections] == [
        ("BACKGROUND", 1),
        ("CONCLUSIONS", 1),
    ]
    assert doc.language == "en"


def test_invalid_xml_raises() -> None:
    with pytest.raises(ParserError, match="invalid_xml"):
        pmc_xml.parse(b"this is not xml at all <")


def test_unrecognized_schema_raises() -> None:
    with pytest.raises(ParserError, match="unrecognized_xml_schema"):
        pmc_xml.parse(b"<catalog><item>x</item></catalog>")


def test_external_entity_not_resolved() -> None:
    payload = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE article [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>\n'
        b"<article><front><article-meta><title-group>"
        b"<article-title>Entity probe</article-title>"
        b"</title-group></article-meta></front>"
        b"<body><sec><title>S</title><p>Before &xxe; after</p></sec></body></article>"
    )
    try:
        doc = pmc_xml.parse(payload)
    except ParserError:
        return  # refusing the document is an acceptable outcome
    text = full_text(doc)
    assert re.search(r"Before\s*(&xxe;)?\s*after", text)
    # No file content may leak between the two markers.
    assert set(text.split()) <= {"Before", "&xxe;", "after"}
