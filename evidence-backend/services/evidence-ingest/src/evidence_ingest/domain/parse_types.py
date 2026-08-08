"""Parser output shapes — frozen in EVA-S02; every parser returns these.

Parsers are pure: bytes in, ParsedDocument out, ParserError on malformed
input. They never execute embedded content (no JS, no macros) and never touch
the network or filesystem — resource limits are enforced by the pipeline
wrapper around them.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ParserError(Exception):
    """Input cannot be parsed; the message is the dead-letter reason."""


class ParsedSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Human-readable section path, e.g. "2. Diagnosis > 2.1 Criteria".
    path: str
    # Heading depth, 1-based; 0 for preamble/body without a heading.
    level: int
    # Section text in reading order. Tables are rendered as
    # tab-separated rows and stay whole inside one section.
    text: str


class ParsedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    # ISO 639-1 when confidently detected ('uk', 'en'), else None.
    language: str | None = None
    sections: list[ParsedSection]
    # Identifier/date/org hints found in the document; well-known keys:
    # doi, pmid, pmcid, isbn, moz_order, published, organization.
    metadata: dict[str, str] = {}


def full_text(doc: ParsedDocument) -> str:
    return "\n\n".join(s.text for s in doc.sections if s.text.strip())
