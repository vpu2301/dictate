"""PII detection for corpus candidates.

The 6 patterns are re-declared from autocomplete-service's scrubber
(services can't import each other; the import-linter independence contract
forbids it — same precedent as scripts/validate-autocomplete-corpus.py).
Drift is guarded by tests/unit/test_pii_drift.py, which reads the
autocomplete-service source and fails if the pattern sets diverge.

Corpus policy is DROP, not redact (ADR-0043 §7): a redacted phrase is
worthless as a completion, and the marker itself leaks that PII was present.
"""

from __future__ import annotations

import re
from typing import Final

# Order matters: more-specific patterns first (mirrors autocomplete_service.scrubber).
PII_PATTERNS: Final[list[tuple[str, re.Pattern[str]]]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("ipn", re.compile(r"\b\d{10}\b")),
    ("med_id", re.compile(r"\b\d{13}\b")),
    ("passport", re.compile(r"\b[A-Za-zА-ЯЇІЄҐа-яїієґ]{2}\s?\d{6}\b")),
    ("dob_like", re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b")),
    ("phone", re.compile(r"(?<![\d+])\+?\d{7,14}(?!\d)")),
]


def contains_pii(text: str) -> list[str]:
    """Names of the patterns that matched; empty list means clean."""
    return [name for name, pattern in PII_PATTERNS if pattern.search(text)]
