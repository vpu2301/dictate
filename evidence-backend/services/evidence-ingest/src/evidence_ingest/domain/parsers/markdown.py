"""Markdown parser: ATX headings, fenced code, pipe tables, YAML-ish front matter.

No external markdown library and no pyyaml — front matter is plain
``key: value`` line parsing only.
"""

from __future__ import annotations

import re

from evidence_ingest.domain.parse_types import ParsedDocument
from evidence_ingest.domain.parsers._shared import (
    SectionAccumulator,
    detect_language,
    extract_common_metadata,
    normalize_date,
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_FENCE_RE = re.compile(r"^(```|~~~)")
_FRONT_MATTER_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.+)$")


def _split_front_matter(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    if not lines or lines[0].strip() != "---":
        return {}, lines
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            fm: dict[str, str] = {}
            for line in lines[1:idx]:
                m = _FRONT_MATTER_KEY_RE.match(line.strip())
                if m:
                    fm[m.group(1).lower()] = m.group(2).strip().strip("\"'")
            return fm, lines[idx + 1 :]
    return {}, lines


def _is_separator_row(line: str) -> bool:
    inner = line.strip().strip("|")
    return "-" in inner and all(ch in "-: \t|" for ch in inner)


def _pipe_row_to_tabs(line: str) -> str:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return "\t".join(cells)


def parse(data: bytes) -> ParsedDocument:
    text = data.decode("utf-8", errors="replace")
    front_matter, body = _split_front_matter(text.splitlines())

    acc = SectionAccumulator()
    first_h1: str | None = None
    in_fence = False
    fence = ""
    for line in body:
        stripped = line.strip()
        if in_fence:
            acc.add(line)
            if stripped.startswith(fence):
                in_fence = False
            continue
        fence_match = _FENCE_RE.match(stripped)
        if fence_match:
            in_fence = True
            fence = fence_match.group(1)
            acc.add(line)
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if level == 1 and first_h1 is None:
                first_h1 = title
            acc.heading(level, title)
            continue
        if stripped.startswith("|"):
            if not _is_separator_row(stripped):
                acc.add(_pipe_row_to_tabs(stripped))
            continue
        if stripped:
            acc.add(stripped)
    sections = acc.sections()

    text_all = "\n".join(s.text for s in sections)
    meta = extract_common_metadata(text_all)
    if "doi" in front_matter:
        meta["doi"] = front_matter["doi"]
    if "date" in front_matter:
        meta["published"] = normalize_date(front_matter["date"]) or front_matter["date"]

    return ParsedDocument(
        title=front_matter.get("title") or first_h1,
        language=detect_language(text_all),
        sections=sections,
        metadata=meta,
    )
