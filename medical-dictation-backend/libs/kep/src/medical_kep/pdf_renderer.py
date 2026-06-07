"""PDF rendering for signed reports.

Sprint-09 produces a deterministic PDF/A-3-styled rendering of each
signed report. The PDF embeds the canonical JSON as a file attachment
so a verifier can extract the structured payload that was signed
(ADR-0022, ADR-0024).

Determinism:
- Jinja2 template with no time-of-render injection.
- WeasyPrint with explicit pinned settings + ``presentational_hints=False``.
- ``mod_date`` / ``creation_date`` overridden via pypdf metadata write
  so the PDF bytes are stable across renders.
- Same input → byte-equal output. Verified by the day-3 determinism test.

CSS injection defence:
- Jinja2 autoescape on for HTML; we never inject untrusted bytes into
  ``<style>`` blocks (the template has none).
- User strings are clamped to ``MAX_FIELD_LENGTH`` (500 chars) before
  rendering so a malicious 10MB-string can't OOM the renderer.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_FIELD_LENGTH = 500
_DETERMINISTIC_DATE = "D:20260101000000+00'00'"
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@dataclass(frozen=True, slots=True)
class RenderInput:
    title: str
    code: str
    issuer_name: str
    encounter_date: str | None
    primary_author_full_name: str
    co_author_names: list[str]
    patient_full_name_redacted: str | None
    icd10_codes: list[str]
    sections: list[dict[str, Any]]
    finalized_at: str
    language: str = "uk"


def _clamp(s: str | None) -> str:
    if not s:
        return ""
    return s[:MAX_FIELD_LENGTH]


def render_unsigned_pdf(payload: RenderInput) -> bytes:
    """Render the HTML template + WeasyPrint.

    ImportError on weasyprint is caught and re-raised with a clear
    message so unit-test environments that don't install the [pdf]
    extra get a helpful failure mode.
    """
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        from weasyprint import HTML
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PDF rendering requires the [pdf] extra of medical_kep "
            "(install weasyprint + jinja2). Original error: " + str(exc)
        ) from exc

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    tpl = env.get_template("report.html.j2")
    html = tpl.render(
        title=_clamp(payload.title),
        code=_clamp(payload.code),
        issuer=_clamp(payload.issuer_name),
        encounter_date=payload.encounter_date or "",
        primary_author=_clamp(payload.primary_author_full_name),
        co_authors=[_clamp(c) for c in payload.co_author_names],
        patient=_clamp(payload.patient_full_name_redacted),
        icd10=payload.icd10_codes,
        sections=[
            {"key": _clamp(s.get("section_key", "")), "text": _clamp(s.get("text", ""))}
            for s in payload.sections
        ],
        finalized_at=payload.finalized_at,
        language=payload.language,
    )
    pdf_bytes = HTML(string=html, base_url=str(_TEMPLATE_DIR)).write_pdf(
        presentational_hints=False,
    )
    return _normalise_pdf_dates(pdf_bytes)


def embed_canonical_json(pdf_bytes: bytes, canonical_bytes: bytes) -> bytes:
    """Attach the canonical JSON as a PDF EmbeddedFile."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter(clone_from=reader)
    writer.add_attachment("canonical.json", canonical_bytes)
    _stamp_deterministic_dates(writer)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def compute_pdf_hash(pdf_bytes: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(pdf_bytes).digest()


def _normalise_pdf_dates(pdf_bytes: bytes) -> bytes:
    """Rewrite /CreationDate and /ModDate so two renders of the same
    input produce byte-equal PDFs."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter(clone_from=reader)
    _stamp_deterministic_dates(writer)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _stamp_deterministic_dates(writer) -> None:
    try:
        writer.add_metadata(
            {
                "/CreationDate": _DETERMINISTIC_DATE,
                "/ModDate": _DETERMINISTIC_DATE,
                "/Producer": "medical_kep",
                "/Creator": "medical_kep",
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("pdf metadata stamp failed (non-fatal): %s", exc)


def extract_embedded_canonical_json(pdf_bytes: bytes) -> bytes | None:
    """Re-extract the canonical JSON from a PDF — used by the verifier
    + day-3 round-trip test."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    attachments = getattr(reader, "attachments", None)
    if not attachments:
        return None
    blob = attachments.get("canonical.json")
    if not blob:
        return None
    # pypdf returns a list[bytes] per attachment name.
    if isinstance(blob, list) and blob:
        return blob[0]
    if isinstance(blob, (bytes, bytearray)):
        return bytes(blob)
    return None
