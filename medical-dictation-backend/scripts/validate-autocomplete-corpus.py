#!/usr/bin/env python3
"""Sprint-10 CI gate — validate autocomplete corpus files.

Usage::

    uv run python scripts/validate-autocomplete-corpus.py                # all committed seed files
    uv run python scripts/validate-autocomplete-corpus.py file.csv ...   # specific file(s)
    uv run python scripts/validate-autocomplete-corpus.py --emit-sql     # render validated files as seed SQL

Validations (row number + reason per failure; exit 1 on any):

- Shape: ``phrases_*.csv`` columns ``phrase,language,specialty,section_hint``;
  ``snippets_*.json`` array of ``{trigger, expansion, cursor_position, language}``.
- Field constraints mirroring the DB CHECKs (migrations 0023/0024):
  language ∈ {uk, en}; phrase 1–80 chars; trigger 2–32; expansion 1–4000;
  cursor_position within the expansion; no leading/trailing whitespace;
  no control characters.
- Duplicates within and across corpus files (case-insensitive per
  language and kind).
- PII sweep with the scrubber's canonical pattern set (IPN, email,
  13-digit medical id, passport, date-like, phone-like incl. int'l) —
  the corpus must be structurally incapable of memorising patient data.
- Ukrainian typography: uk text uses the ’ apostrophe, not ASCII '.

The PII patterns are re-declared here (a script must not import service
code); ``tests/unit/test_corpus_contract.py`` fails if this set drifts
from ``autocomplete_service.scrubber._PATTERNS``.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEED_DIR = REPO / "infra" / "seeds" / "autocomplete"

LANGUAGES = {"uk", "en"}
PHRASE_MIN, PHRASE_MAX = 1, 80
TRIGGER_MIN, TRIGGER_MAX = 2, 32
EXPANSION_MIN, EXPANSION_MAX = 1, 4000
PROHIBITED_APOSTROPHE = "'"

# Mirror of autocomplete_service.scrubber._PATTERNS — kept in lock-step by
# tests/unit/test_corpus_contract.py. Do not tune here without tuning there.
PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("ipn", re.compile(r"\b\d{10}\b")),
    ("med_id", re.compile(r"\b\d{13}\b")),
    ("passport", re.compile(r"\b[A-Za-zА-ЯЇІЄҐа-яїієґ]{2}\s?\d{6}\b")),
    ("dob_like", re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b")),
    ("phone", re.compile(r"(?<![\d+])\+?\d{7,14}(?!\d)")),
]


def _pii_hits(text: str) -> list[str]:
    return [name for name, pat in PII_PATTERNS if pat.search(text)]


def _text_errors(text: str, *, where: str, language: str) -> list[str]:
    errs: list[str] = []
    if text != text.strip():
        errs.append(f"{where}: leading/trailing whitespace")
    if any(unicodedata.category(c) == "Cc" for c in text):
        errs.append(f"{where}: control character")
    hits = _pii_hits(text)
    if hits:
        errs.append(f"{where}: PII pattern(s) matched: {', '.join(hits)}")
    if language == "uk" and PROHIBITED_APOSTROPHE in text:
        errs.append(f"{where}: use Ukrainian apostrophe ’ not '")
    return errs


def check_phrases_csv(path: Path) -> tuple[list[dict], list[str]]:
    errs: list[str] = []
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    with path.open("r", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        expected = {"phrase", "language", "specialty", "section_hint"}
        if set(reader.fieldnames or []) != expected:
            return [], [f"{path.name}: columns must be {sorted(expected)}"]
        for i, row in enumerate(reader, start=2):
            where = f"{path.name}:{i}"
            phrase = row.get("phrase") or ""
            language = row.get("language") or ""
            if language not in LANGUAGES:
                errs.append(f"{where}: language {language!r} not in {sorted(LANGUAGES)}")
            if not (PHRASE_MIN <= len(phrase) <= PHRASE_MAX):
                errs.append(
                    f"{where}: phrase length {len(phrase)} outside "
                    f"{PHRASE_MIN}–{PHRASE_MAX}"
                )
            errs.extend(_text_errors(phrase, where=where, language=language))
            key = (language, phrase.casefold())
            if key in seen:
                errs.append(f"{where}: duplicate phrase for language {language!r}")
            seen.add(key)
            rows.append(row)
    return rows, errs


def check_snippets_json(path: Path) -> tuple[list[dict], list[str]]:
    errs: list[str] = []
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as e:
        return [], [f"{path.name}: invalid JSON: {e}"]
    if not isinstance(data, list):
        return [], [f"{path.name}: expected a JSON array"]
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i, e in enumerate(data, start=1):
        where = f"{path.name}[{i}]"
        trig = e.get("trigger") or ""
        exp = e.get("expansion") or ""
        language = e.get("language") or ""
        cursor = e.get("cursor_position")
        if language not in LANGUAGES:
            errs.append(f"{where}: language {language!r} not in {sorted(LANGUAGES)}")
        if not (TRIGGER_MIN <= len(trig) <= TRIGGER_MAX):
            errs.append(
                f"{where}: trigger length {len(trig)} outside "
                f"{TRIGGER_MIN}–{TRIGGER_MAX}"
            )
        if not (EXPANSION_MIN <= len(exp) <= EXPANSION_MAX):
            errs.append(
                f"{where}: expansion length {len(exp)} outside "
                f"{EXPANSION_MIN}–{EXPANSION_MAX}"
            )
        if not isinstance(cursor, int) or not (0 <= cursor <= len(exp)):
            errs.append(f"{where}: cursor_position must be an int within the expansion")
        errs.extend(_text_errors(trig, where=where, language=language))
        errs.extend(_text_errors(exp, where=where, language=language))
        key = (language, trig.casefold())
        if key in seen:
            errs.append(f"{where}: duplicate trigger for language {language!r}")
        seen.add(key)
        rows.append(e)
    return rows, errs


def _sql_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def emit_sql(phrases: list[dict], snippets: list[dict]) -> str:
    """Render validated rows in the migration-0026 INSERT shape."""
    lines: list[str] = []
    if phrases:
        lines.append(
            "INSERT INTO autocomplete_phrases "
            "(tenant_id, owner_user_id, phrase, language, specialty, section_hint, source)"
        )
        lines.append("VALUES")
        vals = [
            "    (NULL, NULL, {}, {}, {}, {}, 'system')".format(
                _sql_quote(r["phrase"]),
                _sql_quote(r["language"]),
                _sql_quote(r["specialty"]),
                _sql_quote(r["section_hint"]),
            )
            for r in phrases
        ]
        lines.append(",\n".join(vals))
        lines.append("ON CONFLICT DO NOTHING;")
        lines.append("")
    if snippets:
        lines.append(
            "INSERT INTO autocomplete_snippets "
            "(tenant_id, owner_user_id, trigger, expansion, cursor_position, language, source)"
        )
        lines.append("VALUES")
        vals = [
            "    (NULL, NULL, {}, {}, {}, {}, 'system')".format(
                _sql_quote(r["trigger"]),
                _sql_quote(r["expansion"]),
                r["cursor_position"],
                _sql_quote(r["language"]),
            )
            for r in snippets
        ]
        lines.append(",\n".join(vals))
        lines.append("ON CONFLICT DO NOTHING;")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate autocomplete corpus files")
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="corpus files (phrases_*.csv / snippets_*.json); "
        "default: all committed seed files",
    )
    parser.add_argument(
        "--emit-sql",
        action="store_true",
        help="print the validated corpus as seed-migration SQL on stdout",
    )
    args = parser.parse_args(argv)

    files = args.files or sorted(SEED_DIR.glob("phrases_*.csv")) + sorted(
        SEED_DIR.glob("snippets_*.json")
    )
    if not files:
        print("error: no corpus files found", file=sys.stderr)
        return 1

    phrases: list[dict] = []
    snippets: list[dict] = []
    all_errs: list[str] = []
    for path in files:
        if not path.exists():
            all_errs.append(f"{path}: no such file")
        elif path.suffix == ".csv":
            rows, errs = check_phrases_csv(path)
            phrases.extend(rows)
            all_errs.extend(errs)
        elif path.suffix == ".json":
            rows, errs = check_snippets_json(path)
            snippets.extend(rows)
            all_errs.extend(errs)
        else:
            all_errs.append(f"{path}: unsupported extension (want .csv or .json)")

    # Cross-file duplicate sweep (case-insensitive per language and kind).
    for kind, key_field, rows in (
        ("phrase", "phrase", phrases),
        ("snippet", "trigger", snippets),
    ):
        seen: set[tuple[str, str]] = set()
        for r in rows:
            key = (r.get("language", ""), (r.get(key_field) or "").casefold())
            if key in seen:
                all_errs.append(
                    f"cross-file duplicate {kind} {r.get(key_field)!r} "
                    f"({r.get('language')})"
                )
            seen.add(key)

    if all_errs:
        for e in all_errs:
            print(f"error: {e}", file=sys.stderr)
        print(f"FAIL: {len(all_errs)} problem(s)", file=sys.stderr)
        return 1

    if args.emit_sql:
        sys.stdout.write(emit_sql(phrases, snippets))
        return 0

    by: dict[tuple[str, str], int] = {}
    for r in phrases:
        by[(r["language"], "phrase")] = by.get((r["language"], "phrase"), 0) + 1
    for r in snippets:
        by[(r["language"], "snippet")] = by.get((r["language"], "snippet"), 0) + 1
    summary = ", ".join(f"{lang}/{kind}: {n}" for (lang, kind), n in sorted(by.items()))
    print(f"ok: autocomplete corpus validated — {summary} — ready to load")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
