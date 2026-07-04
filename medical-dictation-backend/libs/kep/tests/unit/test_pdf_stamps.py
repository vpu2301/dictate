"""Signed-artifact PDF stamps — dev watermark + qualified КЕП block.

Self-skips when the WeasyPrint native stack (pango/cairo) is not
installed (macOS dev machines); runs fully in the Linux images and on
``RUN_PDF_RENDER=1`` environments. Deliverable asserts (spec VERIFY):
renders are byte-equal across repeats, the dev watermark text appears
on EVERY page, and the embedded canonical.json round-trips.
"""

from __future__ import annotations

import io

import pytest
from medical_kep.pdf_renderer import (
    RenderInput,
    SignatureStamp,
    extract_embedded_canonical_json,
    render_signed_pdf,
)


def _native_render_available() -> bool:
    try:
        import weasyprint  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 — native lib load errors aren't ImportError
        return False


pytestmark = pytest.mark.skipif(
    not _native_render_available(),
    reason="weasyprint native stack (pango/cairo) unavailable",
)

CANONICAL = b'{"canonical_version":"1.0","probe":"pdf"}'


def _payload() -> RenderInput:
    # Two long sections force multiple pages so the every-page watermark
    # claim is actually exercised.
    long_text = ("Пацієнт скаржиться на головний біль. " * 60).strip()
    return RenderInput(
        title="Консультативний висновок",
        code="REP-2026-00042",
        issuer_name="Клініка Тест",
        encounter_date="2026-07-01",
        primary_author_full_name="Лікар Тестовий Олексійович",
        co_author_names=[],
        patient_full_name_redacted="П***нко І.І.",
        icd10_codes=["G43.0"],
        sections=[
            {"section_key": "skarhy", "text": long_text},
            {"section_key": "anamnez", "text": long_text},
            {"section_key": "diagnoz", "text": long_text},
        ],
        finalized_at="2026-07-02T10:00:00+00:00",
        language="uk",
    )


def _dev_stamp() -> SignatureStamp:
    return SignatureStamp(
        level="dev",
        signer_full_name="Лікар Тестовий",
        provider_label="dev_password (development scaffold)",
        signed_at="2026-07-02T10:05:00+00:00",
    )


def test_renders_are_byte_equal() -> None:
    first = render_signed_pdf(_payload(), stamp=_dev_stamp(), canonical_bytes=CANONICAL)
    for _ in range(4):
        again = render_signed_pdf(_payload(), stamp=_dev_stamp(), canonical_bytes=CANONICAL)
        assert again == first


def test_dev_watermark_on_every_page() -> None:
    from pypdf import PdfReader

    pdf = render_signed_pdf(_payload(), stamp=_dev_stamp(), canonical_bytes=CANONICAL)
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) >= 2, "fixture must span multiple pages"
    for page in reader.pages:
        # extract_text wraps lines arbitrarily — normalise whitespace.
        text = " ".join((page.extract_text() or "").split())
        assert "NOT LEGALLY SIGNED" in text, "dev watermark missing from a page"


def test_embedded_canonical_json_round_trips() -> None:
    pdf = render_signed_pdf(_payload(), stamp=_dev_stamp(), canonical_bytes=CANONICAL)
    assert extract_embedded_canonical_json(pdf) == CANONICAL


def test_qualified_stamp_carries_kep_block() -> None:
    from pypdf import PdfReader

    pdf = render_signed_pdf(
        _payload(),
        stamp=SignatureStamp(
            level="qualified",
            signer_full_name="Лікар Кваліфікований",
            provider_label="file_key (КНЕДП)",
            signed_at="2026-07-02T10:05:00+00:00",
        ),
        canonical_bytes=CANONICAL,
    )
    reader = PdfReader(io.BytesIO(pdf))
    full_text = "\n".join(p.extract_text() or "" for p in reader.pages)
    assert "Кваліфікований електронний підпис" in full_text
    assert "Лікар Кваліфікований" in full_text
    assert "NOT LEGALLY SIGNED" not in full_text
