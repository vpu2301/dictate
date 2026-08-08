"""HTML guideline parser: BeautifulSoup (html.parser), h1..h6 section tree."""

from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import Tag

from evidence_ingest.domain.parse_types import ParsedDocument, ParsedSection, ParserError
from evidence_ingest.domain.parsers._shared import (
    SectionAccumulator,
    detect_language,
    extract_common_metadata,
    normalize_date,
)

_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form")
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def _clean(text: str) -> str:
    return " ".join(text.split())


def _meta_content(soup: BeautifulSoup, attrs: dict[str, str]) -> str | None:
    el = soup.find("meta", attrs=attrs)
    if isinstance(el, Tag):
        content = el.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def _render_table(table: Tag) -> str:
    rows: list[str] = []
    for tr in table.find_all("tr"):
        cells = [_clean(c.get_text()) for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append("\t".join(cells))
    return "\n".join(rows)


def parse(data: bytes) -> ParsedDocument:
    soup = BeautifulSoup(data, "html.parser")
    for name in _STRIP_TAGS:
        for junk in soup.find_all(name):
            junk.decompose()

    meta: dict[str, str] = {}
    doi = _meta_content(soup, {"name": "citation_doi"})
    if doi:
        meta["doi"] = doi
    date = _meta_content(soup, {"name": "citation_date"})
    if date:
        meta["published"] = normalize_date(date) or date
    site = _meta_content(soup, {"property": "og:site_name"})
    if site:
        meta["organization"] = site

    title_el = soup.find("title")
    h1_el = soup.find("h1")
    title = _clean(title_el.get_text()) if title_el is not None else ""
    if not title and h1_el is not None:
        title = _clean(h1_el.get_text())

    acc = SectionAccumulator()
    for el in soup.find_all([*_HEADINGS, "p", "li", "table"]):
        if not isinstance(el, Tag) or el.find_parent("table") is not None:
            continue
        if el.name in _HEADINGS:
            acc.heading(int(el.name[1]), _clean(el.get_text()))
        elif el.name == "table":
            rendered = _render_table(el)
            if rendered:
                acc.add(rendered)
        else:
            text = _clean(el.get_text())
            if text:
                acc.add(text)
    sections = acc.sections()

    if not sections:
        body = soup.body if soup.body is not None else soup
        text = _clean(body.get_text(" "))
        if not text:
            raise ParserError("no_extractable_text")
        sections = [ParsedSection(path="", level=0, text=text)]

    text_all = "\n".join(s.text for s in sections)
    return ParsedDocument(
        title=title or None,
        language=detect_language(text_all),
        sections=sections,
        metadata={**extract_common_metadata(text_all), **meta},
    )
