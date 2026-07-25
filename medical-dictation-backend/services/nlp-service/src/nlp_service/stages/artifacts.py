"""Structured artifacts the normalizer stages report about their own output.

Sprint 13. The field-extraction binder must not re-derive what the
sprint-05 normalizers already decided: spoken-numeral logic
("сто сорок" → 140) and relative-date resolution ("три дні тому" →
an ISO date) live in exactly one place each, and a binder that
re-implemented either would drift the moment a normalizer changed.

So each normalizer stage reports what it produced, reading **its own
canonical output vocabulary** (the same ``_UNITS`` values it writes,
the separators it was handed). The consumer does no parsing at all —
it picks from this list. A test asserts the binder carries no numeral
or unit vocabulary of its own.
"""

from __future__ import annotations

import re
from typing import Final

from ..pipeline.base import DateArtifact, NumericArtifact

# ``YYYY-MM-DD`` — the only date form the date normalizer emits.
_ISO_DATE_RE: Final = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def numeric_artifacts_from_output(
    normalized_text: str,
    *,
    decimal_separator: str,
    canonical_units: frozenset[str],
) -> tuple[NumericArtifact, ...]:
    """Read back the measurements the number normalizer just wrote.

    A measurement is a numeric token (already digits at this point —
    the normalizer converted them) optionally followed by one of the
    normalizer's own canonical unit strings. Multi-word units ("мм рт.
    ст.") are matched longest-first so "мм" never wins over "мм рт. ст.".
    """
    tokens = normalized_text.split()
    # Longest first so multi-token units match before their prefixes.
    units_by_len = sorted(canonical_units, key=lambda u: -len(u.split()))

    number_re = re.compile(rf"^\d+(?:{re.escape(decimal_separator)}\d+)?$")
    artifacts: list[NumericArtifact] = []
    for index, token in enumerate(tokens):
        stripped = token.rstrip(",;:.")
        if not number_re.match(stripped):
            continue
        unit = ""
        rendered = stripped
        remainder = " ".join(tokens[index + 1 :])
        for candidate in units_by_len:
            if remainder == candidate or remainder.startswith(candidate + " "):
                unit = candidate
                rendered = f"{stripped} {candidate}"
                break
        artifacts.append(
            NumericArtifact(
                value=stripped,
                unit=unit,
                rendered=rendered,
                token_index=index,
            )
        )
    return tuple(artifacts)


def date_artifacts_from_output(normalized_text: str) -> tuple[DateArtifact, ...]:
    """Read back the ISO dates the date normalizer just wrote.

    Relative dates are only present here if the normalizer resolved
    them — this function never resolves anything itself.
    """
    return tuple(
        DateArtifact(iso=match.group(1), char_index=match.start())
        for match in _ISO_DATE_RE.finditer(normalized_text)
    )
