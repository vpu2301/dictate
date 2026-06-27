"""Unsigned-PDF rendering for local-KEP signing (M1·A3).

Builds a ``medical_kep.pdf_renderer.RenderInput`` from a report + version
row and calls the shared renderer. The weasyprint/jinja2 import is lazy
inside ``render_unsigned_pdf`` so it stays out of the router import path —
only an actual render touches the native deps.

Name resolution is deliberately minimal (doc 02·A3): the author UUID is
the ``primary_author_full_name`` fallback and the patient is redacted —
report-service does not hold the patient name aggregate.
"""

from __future__ import annotations

from medical_kep.pdf_renderer import RenderInput, render_unsigned_pdf

from .reports_repository import ReportRow, VersionRow


def build_render_input(
    *, report: ReportRow, version: VersionRow, issuer_name: str
) -> RenderInput:
    content = version.content
    encounter_date = content.encounter_date
    if encounter_date is None and report.encounter_date is not None:
        encounter_date = report.encounter_date.date().isoformat()

    # ``finalized_at`` is required by the renderer; A3 only renders finalized
    # reports so the caller guarantees it is set.
    finalized_at = report.finalized_at.isoformat() if report.finalized_at else ""

    return RenderInput(
        title=report.title,
        code=report.code,
        issuer_name=issuer_name,
        encounter_date=encounter_date,
        primary_author_full_name=str(report.primary_author_id),
        co_author_names=[str(a) for a in report.co_author_ids],
        patient_full_name_redacted="[redacted]",
        icd10_codes=list(report.icd10_codes),
        sections=[{"section_key": s.section_key, "text": s.text} for s in content.sections],
        finalized_at=finalized_at,
    )


def render_report_pdf(*, report: ReportRow, version: VersionRow, issuer_name: str) -> bytes:
    """Render the unsigned PDF bytes for a finalized report version."""
    payload = build_render_input(report=report, version=version, issuer_name=issuer_name)
    return render_unsigned_pdf(payload)  # type: ignore[no-any-return]
