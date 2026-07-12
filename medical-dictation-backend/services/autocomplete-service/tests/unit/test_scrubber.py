"""PII scrubber regex coverage — the privacy surface."""

from __future__ import annotations

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


def test_intl_phone_redacted():
    # Regression: +380501234567 is a 12-digit run — used to fall between
    # the exact-10 IPN and exact-13 med-id patterns and leak verbatim.
    out = scrub_prefix("пацієнт тел +380501234567 вранці")
    assert "380501234567" not in out.text
    assert out.redactions["phone"] == 1


def test_11_and_12_digit_runs_redacted():
    for run in ("38050123456", "380501234567"):
        out = scrub_prefix(f"номер {run} у файлі")
        assert run not in out.text, f"leaked: {run}"


def test_vitals_not_scrubbed():
    safe = "АТ 120/80 мм рт ст, ЧСС 72 за хвилину"
    out = scrub_prefix(safe)
    assert out.text == safe


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
        "мобільний +380501234567",
        "тел 380501234567",
    ]
    for c in cases:
        out = scrub_prefix(c)
        assert REDACTED in out.text, f"PII not scrubbed: {c!r}"


def test_passport_latin_no_space_redacted():
    # §8: passport both as "АБ 123456" (covered above) and "AB123456".
    out = scrub_prefix("passport AB123456 on file")
    assert "AB123456" not in out.text
    assert out.redactions.get("passport") == 1
