"""The PII sweep must flag planted patterns and pass clean transcripts."""

from __future__ import annotations

from pathlib import Path

import check_corpus_pii


def test_flags_ten_digit_ipn():
    findings = check_corpus_pii.sweep_text("Пацієнт ІПН 1234567890.", Path("t.txt"))
    assert any("IPN" in f for f in findings)


def test_flags_context_term_with_digits():
    findings = check_corpus_pii.sweep_text("номер 1234567", Path("t.txt"))
    assert any("context+digits" in f for f in findings)


def test_clean_clinical_text_has_no_findings():
    # Clinical numbers (BP, dose) must NOT trip the PII sweep.
    text = "АТ 120/80 мм рт ст, бісопролол 5 мг 1 раз на добу."
    assert check_corpus_pii.sweep_text(text, Path("t.txt")) == []


def test_shipped_corpus_is_clean():
    # The 8 placeholder fixtures must pass the PII gate as committed.
    corpus = Path(__file__).resolve().parents[3] / "eval" / "corpus" / "v1"
    findings: list[str] = []
    for tpath in corpus.rglob("transcript.txt"):
        findings.extend(check_corpus_pii.sweep_text(tpath.read_text("utf-8"), tpath))
    assert findings == []
