"""Readability-style main-content extraction + garbage detection.

The rule from the sprint brief: **prefer skipping a page over ingesting
garbage**. A nav-bar soup that reaches the synthesis prompt produces a fluent
citation of nothing, which is worse than one fewer source. Every skip is a
reason string that lands in the answer trace.

Approach (no external readability dependency — the heuristics are small and
we want them testable and deterministic):

1. Strip non-content elements outright (script/style/nav/header/footer/aside,
   forms, ads, cookie banners by role/class).
2. Score every candidate block by text length minus link-text length, with a
   bonus for paragraph density; pick the highest-scoring subtree.
3. Reject the result if it is too short, too link-dense, or reads like a
   paywall/consent interstitial.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "form",
    "nav",
    "header",
    "footer",
    "aside",
    "button",
    "select",
    "option",
)
_DROP_PATTERN = re.compile(
    r"(^|[\s_-])(nav|menu|sidebar|breadcrumb|cookie|consent|banner|advert|ads?|"
    r"promo|social|share|comment|related|footer|header|subscribe|newsletter|"
    r"paywall|skip-link)([\s_-]|$)",
    re.IGNORECASE,
)
_CANDIDATE_TAGS = ("article", "main", "section", "div", "body")

_PAYWALL_MARKERS = (
    "subscribe to continue",
    "subscription required",
    "sign in to read",
    "purchase access",
    "get full access",
    "this content is for subscribers",
    "institutional access",
    "buy this article",
    "log in to view",
)
_CONSENT_MARKERS = (
    "we use cookies",
    "accept all cookies",
    "manage your cookie",
    "enable javascript",
    "javascript is disabled",
    "please enable javascript",
    "checking your browser",
    "verify you are human",
)


class SkipReason:
    too_short = "extract_too_short"
    link_soup = "extract_link_density"
    paywall = "extract_paywall"
    consent_wall = "extract_consent_or_js_wall"
    no_content = "extract_no_content_node"


@dataclass(frozen=True, slots=True)
class Extraction:
    title: str
    text: str
    # Non-empty when the page was rejected; `text` is then the best-effort
    # remainder, kept only for the trace, never for synthesis.
    skip_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.skip_reason


def _looks_boilerplate(node: Tag) -> bool:
    for attribute in ("class", "id", "role"):
        value = node.get(attribute)
        if value is None:
            continue
        text = " ".join(value) if isinstance(value, list) else str(value)
        if _DROP_PATTERN.search(text):
            return True
    return False


def _block_text(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _score(node: Tag) -> float:
    text = _block_text(node)
    if not text:
        return 0.0
    link_chars = sum(len(_block_text(a)) for a in node.find_all("a"))
    paragraphs = len(node.find_all("p"))
    return (len(text) - link_chars) + paragraphs * 25.0


def _link_density(node: Tag) -> float:
    text = _block_text(node)
    if not text:
        return 1.0
    link_chars = sum(len(_block_text(a)) for a in node.find_all("a"))
    return link_chars / len(text)


def extract(html: str, *, min_chars: int, max_link_density: float) -> Extraction:
    soup = BeautifulSoup(html, "lxml")

    # The <h1> names the document; <title> usually names the document AND the
    # site ("CAP guideline — WHO"), and the site part is noise in a citation.
    # So h1 wins whenever there is one.
    title = ""
    if soup.title and soup.title.string:
        title = " ".join(str(soup.title.string).split())
    heading = soup.find("h1")
    if isinstance(heading, Tag):
        heading_text = _block_text(heading)
        if heading_text:
            title = heading_text

    # `find_all` returns a snapshot of the tree, so decomposing a container
    # destroys descendants that are still in the list we are walking. bs4
    # clears a destroyed tag's `attrs`, and reading one raises AttributeError —
    # which the connector catches as `page_failed`, i.e. every real page with a
    # boilerplate wrapper is silently dropped. `decomposed` is the check.
    for tag in soup.find_all(_DROP_TAGS):
        if not tag.decomposed:
            tag.decompose()
    for tag in soup.find_all(True):
        if isinstance(tag, Tag) and not tag.decomposed and _looks_boilerplate(tag):
            tag.decompose()

    best: Tag | None = None
    best_score = 0.0
    for tag_name in _CANDIDATE_TAGS:
        for node in soup.find_all(tag_name):
            if not isinstance(node, Tag):
                continue
            score = _score(node)
            if score > best_score:
                best, best_score = node, score
    if best is None:
        return Extraction(title=title, text="", skip_reason=SkipReason.no_content)

    # Paragraph-joined text keeps sentence boundaries the chunker relies on.
    paragraphs = [
        _block_text(p) for p in best.find_all(["p", "li", "h2", "h3", "h4", "td"]) if _block_text(p)
    ]
    text = "\n\n".join(paragraphs) if paragraphs else _block_text(best)
    lowered = text.casefold()

    if any(marker in lowered for marker in _PAYWALL_MARKERS):
        return Extraction(title=title, text=text, skip_reason=SkipReason.paywall)
    if any(marker in lowered for marker in _CONSENT_MARKERS) and len(text) < min_chars * 3:
        return Extraction(title=title, text=text, skip_reason=SkipReason.consent_wall)
    if len(text) < min_chars:
        return Extraction(title=title, text=text, skip_reason=SkipReason.too_short)
    if _link_density(best) > max_link_density:
        return Extraction(title=title, text=text, skip_reason=SkipReason.link_soup)

    return Extraction(title=title, text=text)
