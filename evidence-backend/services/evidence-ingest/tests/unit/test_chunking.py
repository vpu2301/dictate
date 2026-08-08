from __future__ import annotations

from evidence_ingest.domain.chunking import chunk_document
from evidence_ingest.domain.parse_types import ParsedDocument, ParsedSection, full_text


def _doc(sections: list[tuple[str, int, str]]) -> ParsedDocument:
    return ParsedDocument(
        sections=[ParsedSection(path=p, level=lv, text=t) for p, lv, t in sections]
    )


def test_small_sections_stay_whole() -> None:
    doc = _doc([("1. Intro", 1, "Short intro."), ("2. Body", 1, "Short body.")])
    chunks = chunk_document(doc)
    assert [c.section_path for c in chunks] == ["1. Intro", "2. Body"]


def test_chunks_never_cross_headings() -> None:
    long_text = "Sentence one is here. " * 400
    doc = _doc([("1. A", 1, long_text), ("2. B", 1, "Tiny.")])
    chunks = chunk_document(doc)
    assert all(c.section_path in ("1. A", "2. B") for c in chunks)
    a_chunks = [c for c in chunks if c.section_path == "1. A"]
    assert len(a_chunks) > 1  # long section split
    assert [c for c in chunks if c.section_path == "2. B"][0].text == "Tiny."


def test_long_section_respects_max_tokens() -> None:
    long_text = "Word " * 50 + ". "
    doc = _doc([("1. A", 1, (long_text * 40).strip())])
    chunks = chunk_document(doc, target_tokens=500, max_tokens=700, min_tokens=350)
    for chunk in chunks:
        assert len(chunk.text.split()) <= 700 + 60  # sentence-boundary slack


def test_tables_kept_whole_even_when_oversized() -> None:
    table = "\n".join("cell\tcell\tcell\tcell" for _ in range(900))
    doc = _doc([("3. Dosing table", 2, table)])
    chunks = chunk_document(doc)
    assert len(chunks) == 1
    assert chunks[0].text == table


def test_offsets_index_into_full_text() -> None:
    doc = _doc([("1. A", 1, "Alpha beta gamma."), ("2. B", 1, "Delta epsilon.")])
    text = full_text(doc)
    for chunk in chunk_document(doc):
        assert text[chunk.char_start : chunk.char_end] == chunk.text


def test_empty_sections_skipped() -> None:
    doc = _doc([("1. A", 1, "   "), ("2. B", 1, "Real content.")])
    chunks = chunk_document(doc)
    assert len(chunks) == 1
    assert chunks[0].section_path == "2. B"
