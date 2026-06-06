#!/usr/bin/env python3
"""Sprint-10 day-2 CI gate — validate the autocomplete seed corpus.

Checks:
- Each phrase ≤ 80 chars.
- No leading/trailing whitespace.
- No duplicate ``(phrase, language, specialty, section_hint)``.
- No 10-digit numbers (potential IPN) — flagged for clinical review.
- No emails.
- Cyrillic phrases use Ukrainian-correct apostrophes (’ not ').
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parents[1] / "infra" / "seeds" / "autocomplete"
MAX_LEN = 80
IPN_RE = re.compile(r"\b\d{10}\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
PROHIBITED_APOSTROPHE = "'"


def _check_phrase(row: dict, lineno: int) -> list[str]:
    out: list[str] = []
    phrase = row.get("phrase", "")
    if len(phrase) > MAX_LEN:
        out.append(f"line {lineno}: phrase too long ({len(phrase)}>{MAX_LEN})")
    if phrase != phrase.strip():
        out.append(f"line {lineno}: leading/trailing whitespace")
    if IPN_RE.search(phrase):
        out.append(f"line {lineno}: contains 10-digit IPN-like number")
    if EMAIL_RE.search(phrase):
        out.append(f"line {lineno}: contains email")
    if row.get("language") == "uk" and PROHIBITED_APOSTROPHE in phrase:
        out.append(f"line {lineno}: use Ukrainian apostrophe ’ not '")
    return out


def _check_csv(path: Path) -> list[str]:
    errs: list[str] = []
    seen: set[tuple] = set()
    with path.open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=2):
            errs.extend(_check_phrase(row, i))
            key = (
                row.get("phrase", ""), row.get("language", ""),
                row.get("specialty", ""), row.get("section_hint", ""),
            )
            if key in seen:
                errs.append(f"line {i}: duplicate {key}")
            seen.add(key)
    return errs


def _check_snippets(path: Path) -> list[str]:
    errs: list[str] = []
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, list):
        return [f"{path}: expected JSON array"]
    triggers: set[str] = set()
    for i, e in enumerate(data, start=1):
        trig = e.get("trigger", "")
        if trig in triggers:
            errs.append(f"{path}[{i}]: duplicate trigger {trig!r}")
        triggers.add(trig)
        exp = e.get("expansion", "")
        if not exp:
            errs.append(f"{path}[{i}]: empty expansion")
        if len(exp) > 4000:
            errs.append(f"{path}[{i}]: expansion > 4000 chars")
    return errs


def main() -> int:
    if not SEED_DIR.exists():
        print(f"warn: corpus dir {SEED_DIR} not present; skipping", file=sys.stderr)
        return 0
    all_errs: list[str] = []
    for csv_path in sorted(SEED_DIR.glob("phrases_*.csv")):
        all_errs.extend(_check_csv(csv_path))
    for snip_path in sorted(SEED_DIR.glob("snippets_*.json")):
        all_errs.extend(_check_snippets(snip_path))
    if all_errs:
        print("=== Autocomplete corpus validation failed ===", file=sys.stderr)
        for e in all_errs:
            print("  " + e, file=sys.stderr)
        return 1
    print("ok: autocomplete corpus validated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
