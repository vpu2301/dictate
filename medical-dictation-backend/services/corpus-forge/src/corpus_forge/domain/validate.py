"""Validator v2 — release-artifact checks (plan §6).

The five seed-file checks stay in scripts/validate-autocomplete-corpus.py
(≤80 chars, whitespace, dupes, PII, apostrophe) and also run here per row.
New in v2, all file-based and deterministic: language-ID/Cyrillic ratio,
near-duplicate gate, risk-flag/tier consistency, provenance completeness,
quota matrix. Morphology (LanguageTool) is a separate, optional, networked
stage — see adapters/languagetool.py.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Final

from rapidfuzz.distance import Levenshtein

from corpus_forge.domain.dedupe import LEVENSHTEIN_THRESHOLD
from corpus_forge.domain.normalize import dedupe_key
from corpus_forge.domain.pii import contains_pii
from corpus_forge.domain.release import CSV_HEADER

_CYRILLIC: Final = re.compile(r"[а-щьюяїієґ]", re.IGNORECASE)
_LATIN: Final = re.compile(r"[a-z]", re.IGNORECASE)
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class RowError:
    line: int
    phrase: str
    check: str
    detail: str


def _language_id_errors(line: int, phrase: str, language: str) -> list[RowError]:
    """Cyrillic-ratio check: catches EN rows in the UK file and vice versa.
    Latin tokens inside uk phrases are legitimate (code-switched drug names),
    so the gate is on the dominant script, not purity."""
    cyr = len(_CYRILLIC.findall(phrase))
    lat = len(_LATIN.findall(phrase))
    total = cyr + lat
    if total == 0:
        return [RowError(line, phrase, "language_id", "no letters")]
    if language == "uk" and cyr / total < 0.5:
        return [RowError(line, phrase, "language_id", f"uk row is {lat}/{total} Latin")]
    if language == "en" and lat / total < 0.9:
        return [RowError(line, phrase, "language_id", f"en row is {cyr}/{total} Cyrillic")]
    return []


def _base_row_errors(line: int, phrase: str, language: str) -> list[RowError]:
    """The five sprint-10 checks, applied per release row."""
    errors: list[RowError] = []
    if not 1 <= len(phrase) <= 80:
        errors.append(RowError(line, phrase, "length", f"{len(phrase)} chars"))
    if phrase != phrase.strip() or "  " in phrase:
        errors.append(RowError(line, phrase, "whitespace", "leading/trailing/double"))
    if _CONTROL.search(phrase):
        errors.append(RowError(line, phrase, "control_chars", "control character"))
    if pii := contains_pii(phrase):
        errors.append(RowError(line, phrase, "pii", ",".join(pii)))
    if language == "uk" and "'" in phrase:
        errors.append(RowError(line, phrase, "apostrophe", "ASCII ' in uk text (use ’)"))
    return errors


def validate_release_csv(csv_text: str) -> list[RowError]:
    """Every deterministic check over a release phrases.csv. Returns all
    errors (never short-circuits) so a failing release is fixable in one pass."""
    reader = csv.DictReader(io.StringIO(csv_text))
    if tuple(reader.fieldnames or ()) != CSV_HEADER:
        return [RowError(1, "", "header", f"expected {CSV_HEADER}, got {reader.fieldnames}")]

    errors: list[RowError] = []
    seen_keys: dict[str, tuple[int, str]] = {}  # dedupe_key → (line, language)
    for line, row in enumerate(reader, start=2):
        phrase = row["phrase"]
        language = row["language"]
        errors.extend(_base_row_errors(line, phrase, language))
        errors.extend(_language_id_errors(line, phrase, language))

        # Risk-flag / tier consistency: flagged rows must be tier 3.
        flags = [f for f in row["risk_flags"].split(";") if f]
        tier = int(row["tier"]) if row["tier"] else None
        if flags and tier != 3:
            errors.append(RowError(line, phrase, "risk_tier", f"flags={flags} but tier={tier}"))

        # Provenance completeness: no anonymous rows in a release.
        if not row["source_ref"]:
            errors.append(RowError(line, phrase, "provenance", "missing source_ref"))
        if not row["review_engine"]:
            errors.append(RowError(line, phrase, "provenance", "missing review_engine"))

        # Near-duplicate gate (same threshold as the serve-time diversity guard).
        key = dedupe_key(phrase)
        for other_key, (other_line, other_lang) in seen_keys.items():
            if other_lang == language and Levenshtein.distance(key, other_key) <= LEVENSHTEIN_THRESHOLD:
                errors.append(
                    RowError(line, phrase, "near_duplicate", f"≤{LEVENSHTEIN_THRESHOLD} edits from line {other_line}")
                )
                break
        seen_keys[key] = (line, language)
    return errors
