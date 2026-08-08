"""PDF parser: pypdf text extraction with heading-line section detection."""

from __future__ import annotations

import contextlib
import io
import re

from pypdf import PdfReader

from evidence_ingest.domain.parse_types import ParsedDocument, ParserError
from evidence_ingest.domain.parsers._shared import (
    SectionAccumulator,
    detect_language,
    extract_common_metadata,
)

_NUMBERED_RE = re.compile(r"^\d+(\.\d+)*\s+\S")
_UKR_HEADING_RE = re.compile(r"^(розділ|додаток)\b", re.IGNORECASE)


def _is_all_caps_heading(line: str) -> bool:
    if not 2 <= len(line) <= 60:
        return False
    letters = [ch for ch in line if ch.isalpha()]
    return len(letters) >= 3 and all(ch.isupper() for ch in letters)


def _heading_level(line: str) -> int | None:
    if _NUMBERED_RE.match(line):
        return line.split()[0].count(".") + 1
    if _UKR_HEADING_RE.match(line) or _is_all_caps_heading(line):
        return 1
    return None


def parse(data: bytes) -> ParsedDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ParserError("encrypted_pdf")
        page_texts = [page.extract_text() or "" for page in reader.pages]
    except ParserError:
        raise
    except Exception as exc:
        raise ParserError("pdf_read_failed") from exc

    text = "\n".join(page_texts)
    if not text.strip():
        raise ParserError("no_extractable_text")

    acc = SectionAccumulator()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        level = _heading_level(line)
        if level is None:
            acc.add(line)
        else:
            acc.heading(level, line)
    sections = acc.sections()

    title: str | None = None
    with contextlib.suppress(Exception):
        if reader.metadata is not None and reader.metadata.title:
            title = str(reader.metadata.title)
    if title is None:
        title = next((ln.strip() for ln in text.splitlines() if ln.strip()), None)

    return ParsedDocument(
        title=title,
        language=detect_language(text),
        sections=sections,
        metadata=extract_common_metadata(text),
    )
