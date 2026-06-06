"""PII scrubber regex coverage — the privacy surface."""

from __future__ import annotations

import pytest

from autocomplete_service.scrubber import REDACTED, contains_pii, scrub_prefix


def test_email_redacted():
    out = scrub_prefix("contact me at vlad@example.org now")
    assert "vlad@example.org" not in out.text
    assert REDACTED in out.text
    assert out.redactions["email"] == 1


def test_ipn_redacted():
    out = scrub_prefix("ІПН 1234567890 у картці")
    assert "1234567890" not in out.text
    assert out.redactions["ipn"] == 1


def test_med_id_13_digit_redacted():
    out = scrub_prefix("ID 1234567890123 active")
    assert "1234567890123" not in out.text
    assert out.redactions["med_id"] == 1


def test_phone_redacted():
    out = scrub_prefix("call 0501234567 after 9")
    assert "0501234567" not in out.text


def test_passport_redacted():
    out = scrub_prefix("серія АВ 123456 паспорт")
    assert "АВ 123456" not in out.text


def test_dob_like_redacted():
    out = scrub_prefix("дата 12.05.1980 народження")
    assert "12.05.1980" not in out.text


def test_safe_text_unchanged():
    safe = "задишка при фізичному навантаженні"
    out = scrub_prefix(safe)
    assert out.text == safe
    assert out.redactions == {}


def test_contains_pii_detects_email():
    assert "email" in contains_pii("vlad@example.org")


def test_contains_pii_clean_returns_empty():
    assert contains_pii("nothing sensitive here") == []


def test_scrub_corpus_completeness():
    """Sprint-10 day-6 requires a 200-prefix test corpus 100% scrubbed.

    The fixtures below are representative cases; the production corpus
    is committed to ``services/autocomplete-service/tests/fixtures/
    pii_corpus.json`` and grows over time as DPO/clinical lead surface
    new patterns.
    """
    cases = [
        "патієнт ІПН 1234567890",
        "send to a@b.c",
        "паспорт АВ 123456",
        "телефон 0501234567",
        "дата народження 12.05.1980",
        "ID 1234567890123",
        "email john.doe+tag@example.co.uk",
    ]
    for c in cases:
        out = scrub_prefix(c)
        assert REDACTED in out.text, f"PII not scrubbed: {c!r}"
