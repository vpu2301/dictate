"""Document parsers: pure functions ``parse(data: bytes) -> ParsedDocument``."""

from __future__ import annotations

from evidence_ingest.domain.parsers import docx, html_guideline, markdown, pdf, pmc_xml

__all__ = ["docx", "html_guideline", "markdown", "pdf", "pmc_xml"]
