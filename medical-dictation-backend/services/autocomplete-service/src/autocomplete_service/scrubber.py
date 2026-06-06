"""PII scrubber for telemetry prefixes + phrase writes.

Sprint-10 day-6 surface. Conservative: false positives (over-scrubbing)
are preferred over false negatives (PII leak into telemetry).

Patterns redacted:
- 10-digit unbroken (Ukrainian IPN).
- 7-digit unbroken (phone-like).
- Email (RFC-5322-lite).
- Passport pattern (2 letters + 6 digits).
- 13-digit medical ID.
- Dates in DD.MM.YYYY or DD/MM/YYYY format.

Replacement: ``<redacted_PII>``.

DPO sign-off captured in ``docs/security/autocomplete-pii-scrubber.md``;
regex updates require DPO re-review.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

REDACTED: Final = "<redacted_PII>"

# Order matters: more-specific patterns first so they win the
# substitution before the generic 7-digit pattern eats them.
_PATTERNS: Final[list[tuple[str, re.Pattern[str]]]] = [
    ("email",     re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("ipn",       re.compile(r"\b\d{10}\b")),
    ("med_id",    re.compile(r"\b\d{13}\b")),
    ("passport",  re.compile(r"\b[A-Za-zА-ЯЇІЄҐа-яїієґ]{2}\s?\d{6}\b")),
    ("dob_like",  re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b")),
    ("phone",     re.compile(r"\b\d{7,9}\b")),
]


@dataclass(frozen=True, slots=True)
class ScrubResult:
    text: str
    redactions: dict[str, int]    # pattern_name → count


def scrub_prefix(text: str) -> ScrubResult:
    """Return (scrubbed_text, per-pattern redaction counts)."""
    counts: dict[str, int] = {}
    for name, pat in _PATTERNS:
        new_text, n = pat.subn(REDACTED, text)
        if n:
            counts[name] = n
            text = new_text
    return ScrubResult(text=text, redactions=counts)


def scrub_context(ctx: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in ctx.items():
        if isinstance(v, str):
            out[k] = scrub_prefix(v).text
        elif isinstance(v, Mapping):
            out[k] = scrub_context(v)
        else:
            out[k] = v
    return out


def contains_pii(text: str) -> list[str]:
    """Lightweight detector used by phrase-write rejection.

    Returns the list of pattern names that match (empty = clean).
    """
    found: list[str] = []
    for name, pat in _PATTERNS:
        if pat.search(text):
            found.append(name)
    return found
