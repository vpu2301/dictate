"""JATS/PMC and MEDLINE XML parser (lxml, XXE-safe: entities never resolved)."""

from __future__ import annotations

from lxml import etree

from evidence_ingest.domain.parse_types import ParsedDocument, ParsedSection, ParserError
from evidence_ingest.domain.parsers._shared import (
    SectionAccumulator,
    detect_language,
    extract_common_metadata,
)


def _make_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
        huge_tree=False,
    )


def _localname(el: etree._Element) -> str:
    return etree.QName(el.tag).localname if isinstance(el.tag, str) else ""


def _text(el: etree._Element | None) -> str:
    if el is None:
        return ""
    return " ".join("".join(el.itertext()).split())


def _child_text(el: etree._Element, name: str) -> str:
    return _text(el.find(name))


def _render_table(el: etree._Element) -> str:
    rows: list[str] = []
    for tr in el.iter("tr"):
        cells = [_text(c) for c in tr if _localname(c) in ("th", "td")]
        if cells:
            rows.append("\t".join(cells))
    return "\n".join(rows)


def _walk_children(el: etree._Element, acc: SectionAccumulator, level: int) -> None:
    for child in el:
        if not isinstance(child.tag, str):
            continue
        name = _localname(child)
        if name == "title":
            continue
        if name == "sec":
            title = _child_text(child, "title")
            acc.heading(level + 1, title)
            _walk_children(child, acc, level + 1)
        elif name == "table-wrap":
            acc.add(_render_table(child))
        else:
            text = _text(child)
            if text:
                acc.add(text)


def _parse_jats(root: etree._Element) -> ParsedDocument:
    title = _text(root.find(".//article-meta/title-group/article-title"))
    meta: dict[str, str] = {}
    for aid in root.findall(".//article-meta/article-id"):
        value = _text(aid)
        kind = aid.get("pub-id-type")
        if not value:
            continue
        if kind == "doi":
            meta["doi"] = value
        elif kind == "pmid":
            meta["pmid"] = value
        elif kind == "pmc":
            meta["pmcid"] = value
    pub = root.find(".//article-meta/pub-date")
    if pub is not None:
        year = _child_text(pub, "year")
        month = _child_text(pub, "month")
        day = _child_text(pub, "day")
        if year.isdigit():
            published = year
            if month.isdigit():
                published += f"-{int(month):02d}"
                if day.isdigit():
                    published += f"-{int(day):02d}"
            meta["published"] = published

    acc = SectionAccumulator()
    abstract = root.find(".//article-meta/abstract")
    if abstract is not None:
        acc.heading(1, "Abstract")
        _walk_children(abstract, acc, 1)
    body = root.find("body")
    if body is not None:
        _walk_children(body, acc, 0)
    sections = acc.sections()

    text_all = "\n".join(s.text for s in sections)
    return ParsedDocument(
        title=title or None,
        language=detect_language(text_all),
        sections=sections,
        metadata={**extract_common_metadata(text_all), **meta},
    )


def _parse_medline(root: etree._Element) -> ParsedDocument:
    article = root if _localname(root) == "PubmedArticle" else root.find(".//PubmedArticle")
    if article is None:
        raise ParserError("unrecognized_xml_schema")
    title = _text(article.find(".//ArticleTitle"))
    meta: dict[str, str] = {}
    pmid = _text(article.find(".//MedlineCitation/PMID"))
    if pmid:
        meta["pmid"] = pmid

    sections: list[ParsedSection] = []
    for abstract_text in article.findall(".//Abstract/AbstractText"):
        text = _text(abstract_text)
        if not text:
            continue
        label = abstract_text.get("Label") or "Abstract"
        sections.append(ParsedSection(path=label, level=1, text=text))

    text_all = "\n".join(s.text for s in sections)
    return ParsedDocument(
        title=title or None,
        language=detect_language(text_all),
        sections=sections,
        metadata={**extract_common_metadata(text_all), **meta},
    )


def parse(data: bytes) -> ParsedDocument:
    try:
        root = etree.fromstring(data, _make_parser())
    except etree.XMLSyntaxError as exc:
        raise ParserError("invalid_xml") from exc
    name = _localname(root)
    if name == "article":
        return _parse_jats(root)
    if name in ("PubmedArticleSet", "PubmedArticle"):
        return _parse_medline(root)
    raise ParserError("unrecognized_xml_schema")
