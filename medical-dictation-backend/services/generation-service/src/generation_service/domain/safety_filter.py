"""Output safety filter — completions are linguistic, never clinical.

Sprint-15 rule: a completion may finish a sentence's grammar; it may
NOT supply a blood pressure. Every numeric clinical value, dosage or
ICD-like code in the completion must appear VERBATIM in the text the
clinician already typed, otherwise the completion is dropped (the
endpoint answers 204 and audits ``layer_c.completion.filtered``).

Same regex-sweep family as the nlp-service numeric artifacts: match
classes are enumerated so the filter can report WHICH class fired
(metric label), and the catch-all bare-number pattern runs last so a
"140/90" is reported as blood pressure, not as two bare numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# Order matters: specific classes first; `bare_number` is the catch-all.
# Units cover the dosage/lab vocabulary both scripts use in UA practice.
_UNIT = (
    r"(?:mg|мг|mcg|мкг|g|г|kg|кг|ml|мл|l|л|IU|МО|од|mmol|ммоль|mol|моль|"
    r"mm\s?Hg|мм\s?рт\.?\s?ст\.?|%|°C|уд/хв|bpm)"
)
_PATTERNS: Final[list[tuple[str, re.Pattern[str]]]] = [
    ("blood_pressure", re.compile(r"\b\d{2,3}\s*/\s*\d{2,3}\b")),
    ("dosage", re.compile(rf"\b\d+(?:[.,]\d+)?\s*{_UNIT}", re.IGNORECASE)),
    ("icd_code", re.compile(r"\b[A-Z]\d{2}(?:\.\d{1,2})?\b")),
    ("date_like", re.compile(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b")),
    ("bare_number", re.compile(r"\d+(?:[.,]\d+)?")),
]


@dataclass(slots=True, frozen=True)
class FilterVerdict:
    allowed: bool
    reason: str | None = None  # pattern class that fired, for the metric label
    matched: str | None = None  # offending fragment (audit payload — closed class)


def check_completion(completion: str, *, text_before_cursor: str) -> FilterVerdict:
    """Every clinical-value match in ``completion`` must be a verbatim
    substring of ``text_before_cursor`` — echoing back what the clinician
    already wrote is legitimate grammar; introducing anything numeric is not.
    """
    consumed: list[tuple[int, int]] = []
    for reason, pattern in _PATTERNS:
        for match in pattern.finditer(completion):
            span = match.span()
            # A fragment already attributed to a more specific class (e.g. the
            # "140" inside an approved "140/90") is not re-judged as a bare
            # number.
            if any(span[0] >= s and span[1] <= e for s, e in consumed):
                continue
            if match.group(0) not in text_before_cursor:
                return FilterVerdict(allowed=False, reason=reason, matched=match.group(0))
            consumed.append(span)
    return FilterVerdict(allowed=True)
