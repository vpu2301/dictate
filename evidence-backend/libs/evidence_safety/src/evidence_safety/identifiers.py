"""Identifier-shaped text detection (QS1 support).

Used wherever a string is about to cross a trust boundary it was not authored
for: the intent sanitizer (before a concept is stored) and the web query
builder (before a term leaves the cluster). Both are defense in depth — the
type wall is what actually enforces QS1 — but they are the layer that catches
a model that ignored its instructions.

Design constraint that shapes every pattern here: **clinical terms contain
digits**. `type 2 diabetes`, `CKD stage 4`, `COVID-19`, `ICD-10`, `HbA1c`,
`CYP2C19`, `vitamin B12`, `IL-6`, `HER2`, `500 mg` must all pass. So the
patterns look for shapes that clinical vocabulary does not have: long digit
runs, dates, contact details, and record-number tokens (letters welded to a
run of three or more digits, which is what an MRN or an accession number
looks like and what a drug name never does).

The QS1 taint harness (`tests/qs1/`) is what proved the record-number case
was missing: an `MRN-CANARY-99881` sailed through a digit-run-only check.
"""

from __future__ import annotations

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 6+ consecutive digits: ІПН, record numbers, long account numbers.
    # Clinical terms top out around 4 (e.g. "CYP3A4", "1000 mg").
    ("digit_run", re.compile(r"\d{6,}")),
    # Dates in any common separator form.
    ("date", re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")),
    ("iso_date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("phone", re.compile(r"\+?\d[\d\s()-]{7,}\d")),
    # Record-number token: two or more letters welded (optionally through one
    # separator) to three or more digits — MRN-99881, ACC/12345, AB123456.
    # `COVID-19` and `ICD-10` are safe: two digits, not three.
    (
        "record_number",
        re.compile(r"\b[^\W\d_]{2,}[-_/]?\d{3,}\b", re.UNICODE),
    ),
    # Two digit groups joined by a separator: 123-45-6789, 12/345678.
    ("segmented_number", re.compile(r"\b\d{2,}[-_/]\d{3,}\b")),
)


def identifier_shaped(text: str) -> str | None:
    """Return the name of the first matching pattern, or None if the text is
    free of identifier shapes."""
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return name
    return None


def is_identifier_shaped(text: str) -> bool:
    return identifier_shaped(text) is not None
