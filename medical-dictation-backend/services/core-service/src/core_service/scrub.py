"""PII scrub for free text that LEAVES the clinical trust boundary.

S11 step 08 gap check found exactly one such path in core-service: the
erasure ``rejection_reason`` (operator free text) is echoed into the
audit payload — and the audit convention is ids only, never identity
strings. This module scrubs it on write.

The pattern set is the S10 autocomplete scrubber's, verbatim — one
DPO-reviewed regex family, two deliberate copies (services must not
import services); ``tests/unit/test_scrub_parity.py`` pins byte-for-byte
parity so the sets can never drift apart silently. Any change here or
there is a DPO re-review (docs/security/autocomplete-pii-scrubber.md).

Deliberately NOT scrubbed: clinical content (notes/anamnesis bodies,
report content, transcripts). Those are PHI by design, protected by
RLS + envelope encryption inside the trust boundary — redaction there
would corrupt the medical record. The boundary distinction is
documented in the scrubber spec.
"""

from __future__ import annotations

import re

_REPLACEMENT = "<redacted_PII>"

# VERBATIM copy of the S10 scrubber's pattern set (order included) —
# parity pinned by tests/unit/test_scrub_parity.py.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("ipn", re.compile(r"\b\d{10}\b")),
    ("med_id", re.compile(r"\b\d{13}\b")),
    ("passport", re.compile(r"\b[A-Za-zА-ЯЇІЄҐа-яїієґ]{2}\s?\d{6}\b")),
    ("dob_like", re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b")),
    ("phone", re.compile(r"(?<![\d+])\+?\d{7,14}(?!\d)")),
)


def scrub_free_text(text: str) -> str:
    """Redact PII shapes from free text bound for outside the boundary."""
    for _, pattern in _PATTERNS:
        text = pattern.sub(_REPLACEMENT, text)
    return text
