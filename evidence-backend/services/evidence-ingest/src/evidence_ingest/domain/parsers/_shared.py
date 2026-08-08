"""Private helpers shared by the document parsers.

Language detection, identifier/date regexes, and the heading-trail section
accumulator live here so no parser duplicates them.
"""

from __future__ import annotations

import re

from evidence_ingest.domain.parse_types import ParsedSection

DOI_RE = re.compile(r"\b10\.\d{4,9}/\S+")
MOZ_ORDER_RE = re.compile(r"наказ.{0,40}№\s*([\d\-а-яa-z]+)", re.IGNORECASE | re.DOTALL)
_DATE_DMY_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def detect_language(text: str) -> str | None:
    """'uk' if Cyrillic letters dominate, 'en' if Latin; None under 50 letters."""
    cyrillic = 0
    latin = 0
    for ch in text:
        if "Ѐ" <= ch <= "ӿ":
            cyrillic += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            latin += 1
    if cyrillic + latin < 50:
        return None
    return "uk" if cyrillic > latin else "en"


def normalize_date(raw: str) -> str | None:
    """Normalize dd.mm.yyyy or yyyy-mm-dd to ISO; None if neither."""
    candidate = raw.strip()
    m = _DATE_DMY_RE.fullmatch(candidate)
    if m:
        day, month, year = m.groups()
        return f"{year}-{month}-{day}"
    m = _DATE_ISO_RE.fullmatch(candidate)
    if m:
        return m.group(0)
    return None


def find_published(text: str) -> str | None:
    """First date in the text (dd.mm.yyyy or ISO), normalized to ISO."""
    candidates: list[tuple[int, str]] = []
    m = _DATE_DMY_RE.search(text)
    if m:
        candidates.append((m.start(), f"{m.group(3)}-{m.group(2)}-{m.group(1)}"))
    m = _DATE_ISO_RE.search(text)
    if m:
        candidates.append((m.start(), m.group(0)))
    if not candidates:
        return None
    return min(candidates)[1]


def extract_common_metadata(text: str) -> dict[str, str]:
    """DOI, МОЗ order number, and first date found anywhere in the text."""
    meta: dict[str, str] = {}
    m = DOI_RE.search(text)
    if m:
        meta["doi"] = m.group(0).rstrip(".,;)\"'")
    m = MOZ_ORDER_RE.search(text)
    if m:
        meta["moz_order"] = m.group(1)
    published = find_published(text)
    if published:
        meta["published"] = published
    return meta


class SectionAccumulator:
    """Builds ParsedSections from a linear stream of headings and text blocks.

    Text before the first heading becomes one level-0 section with path "".
    The path of a heading section is the joined trail of open headings.
    """

    def __init__(self) -> None:
        self._trail: list[tuple[int, str]] = []
        self._parts: list[str] = []
        self._sections: list[ParsedSection] = []

    def heading(self, level: int, title: str) -> None:
        self._flush()
        while self._trail and self._trail[-1][0] >= level:
            self._trail.pop()
        self._trail.append((level, title))

    def add(self, text: str) -> None:
        if text:
            self._parts.append(text)

    def sections(self) -> list[ParsedSection]:
        self._flush()
        return list(self._sections)

    def _flush(self) -> None:
        text = "\n".join(self._parts).strip()
        self._parts = []
        if not text and not self._trail:
            return
        path = " > ".join(title for _, title in self._trail)
        level = self._trail[-1][0] if self._trail else 0
        self._sections.append(ParsedSection(path=path, level=level, text=text))
