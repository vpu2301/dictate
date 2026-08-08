"""DOCX parser: python-docx, heading styles (English and Ukrainian), inline tables."""

from __future__ import annotations

import io
import re

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from evidence_ingest.domain.parse_types import ParsedDocument, ParserError
from evidence_ingest.domain.parsers._shared import (
    SectionAccumulator,
    detect_language,
    extract_common_metadata,
)

_HEADING_STYLE_RE = re.compile(r"^(?:Heading|Заголовок)\s+(\d)$")


def _heading_level(paragraph: Paragraph) -> int | None:
    style = paragraph.style
    name = style.name if style is not None else None
    if not name:
        return None
    m = _HEADING_STYLE_RE.match(name)
    return int(m.group(1)) if m else None


def parse(data: bytes) -> ParsedDocument:
    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:
        raise ParserError("invalid_docx") from exc

    acc = SectionAccumulator()
    for child in document.element.body.iterchildren():
        if not isinstance(child.tag, str):
            continue
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            level = _heading_level(paragraph)
            if level is None:
                acc.add(text)
            else:
                acc.heading(level, text)
        elif child.tag.endswith("}tbl"):
            table = Table(child, document)
            rows = ["\t".join(cell.text.strip() for cell in row.cells) for row in table.rows]
            if rows:
                acc.add("\n".join(rows))
    sections = acc.sections()
    if not sections:
        raise ParserError("no_extractable_text")

    text_all = "\n".join(s.text for s in sections)
    meta = extract_common_metadata(text_all)
    core = document.core_properties
    if core.created is not None:
        meta["published"] = core.created.date().isoformat()

    return ParsedDocument(
        title=core.title or None,
        language=detect_language(text_all),
        sections=sections,
        metadata=meta,
    )
