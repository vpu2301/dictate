"""PII detection + drift guard against the autocomplete-service scrubber."""

import re
from pathlib import Path

from corpus_forge.domain.pii import PII_PATTERNS, contains_pii

SCRUBBER = (
    Path(__file__).resolve().parents[3]
    / "autocomplete-service/src/autocomplete_service/scrubber.py"
)


def test_pii_examples_detected() -> None:
    # 10 contiguous digits legitimately match both ipn and the 7-14-digit
    # phone pattern; the corpus policy is drop-on-any-match, so overlap is fine.
    assert contains_pii("пацієнт іванов іпн 1234567890") == ["ipn", "phone"]
    assert "email" in contains_pii("надіслати на dr.house@clinic.ua")
    assert "phone" in contains_pii("телефон +380501234567")
    assert "dob_like" in contains_pii("народився 01.02.1980")
    assert "passport" in contains_pii("паспорт КМ 123456")
    assert "med_id" in contains_pii("запис 1234567890123")


def test_clean_clinical_text_passes() -> None:
    assert contains_pii("встановлено діагноз гіпертонічна хвороба") == []
    assert contains_pii("бісопролол п'ять міліграмів раз на добу") == []


def test_patterns_do_not_drift_from_autocomplete_service() -> None:
    """corpus-forge may not import the service (import-linter independence),
    so the patterns are re-declared; this test fails when the service's
    scrubber changes and this copy doesn't."""
    source = SCRUBBER.read_text(encoding="utf-8")
    service_patterns = re.findall(r'\("(\w+)", re\.compile\(r"(.+?)"\)\)', source)
    ours = [(name, pattern.pattern) for name, pattern in PII_PATTERNS]
    assert service_patterns == ours, "scrubber patterns drifted — sync corpus_forge/domain/pii.py"
