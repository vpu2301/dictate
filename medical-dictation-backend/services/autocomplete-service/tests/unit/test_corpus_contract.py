"""Corpus validator ↔ scrubber contract (sprint-10 step 02).

The validator re-declares the scrubber's PII patterns (a script must not
import service code). These tests fail if the two sets drift, prove the
validator's verdicts on the committed corpus and on planted PII, and pin
migration 0026 to the CSV/JSON source of truth (semantic row equality —
0026 is an applied, checksummed migration, so byte-identity with
generator output is not required).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from autocomplete_service.scrubber import _PATTERNS as SCRUBBER_PATTERNS

REPO = Path(__file__).resolve().parents[4]
VALIDATOR = REPO / "scripts" / "validate-autocomplete-corpus.py"
SEED_DIR = REPO / "infra" / "seeds" / "autocomplete"
MIGRATION_0026 = (
    REPO / "infra" / "postgres" / "migrations"
    / "0026_seed_autocomplete_system_corpus.sql"
)


def _load_validator():
    spec = importlib.util.spec_from_file_location("corpus_validator", VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["corpus_validator"] = mod
    spec.loader.exec_module(mod)
    return mod


validator = _load_validator()


# ── pattern drift ────────────────────────────────────────────────────────


def test_validator_patterns_match_scrubber_exactly():
    """If step-05 tunes a scrubber regex, this forces the validator along."""
    scrubber = {name: pat.pattern for name, pat in SCRUBBER_PATTERNS}
    mirrored = {name: pat.pattern for name, pat in validator.PII_PATTERNS}
    assert mirrored == scrubber


# ── committed corpus verdicts ────────────────────────────────────────────


def test_committed_corpus_validates_clean(capsys):
    assert validator.main([]) == 0
    out = capsys.readouterr().out
    assert "ready to load" in out


def test_planted_ipn_rejected(tmp_path, capsys):
    f = tmp_path / "phrases_x.csv"
    f.write_text(
        "phrase,language,specialty,section_hint\n"
        "пацієнт ІПН 1234567890,uk,general,anamnesis\n",
        encoding="utf-8",
    )
    assert validator.main([str(f)]) == 1
    assert "ipn" in capsys.readouterr().err


def test_planted_intl_phone_rejected(tmp_path, capsys):
    f = tmp_path / "phrases_x.csv"
    f.write_text(
        "phrase,language,specialty,section_hint\n"
        "call patient at +380501234567,en,general,plan\n",
        encoding="utf-8",
    )
    assert validator.main([str(f)]) == 1
    assert "phone" in capsys.readouterr().err


def test_malformed_rows_rejected(tmp_path, capsys):
    f = tmp_path / "phrases_x.csv"
    f.write_text(
        "phrase,language,specialty,section_hint\n"
        "задишка,de,general,anamnesis\n"
        f"{'x' * 81},en,general,plan\n"
        "same phrase,en,general,plan\n"
        "Same Phrase,en,general,plan\n",
        encoding="utf-8",
    )
    assert validator.main([str(f)]) == 1
    err = capsys.readouterr().err
    assert "language 'de'" in err
    assert "length 81" in err
    assert "duplicate" in err


# ── emit-sql round-trip: CSV/JSON is the single source of truth ─────────

_ROW_RE = re.compile(r"^\s*\(NULL, NULL, (.+?)\),?$")


def _split_sql_tuple(body: str) -> list[str]:
    """Split "'a', 'b', 7, 'c'" into fields, honouring '' escapes."""
    fields, buf, in_str, i = [], [], False, 0
    while i < len(body):
        c = body[i]
        if in_str:
            if c == "'":
                if i + 1 < len(body) and body[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_str = False
            else:
                buf.append(c)
        elif c == "'":
            in_str = True
        elif c == ",":
            fields.append("".join(buf).strip())
            buf = []
        elif not c.isspace():
            buf.append(c)
        i += 1
    fields.append("".join(buf).strip())
    return fields


def _rows_from_sql(sql: str) -> set[tuple]:
    rows: set[tuple] = set()
    for line in sql.splitlines():
        m = _ROW_RE.match(line)
        if m:
            rows.add(tuple(_split_sql_tuple(m.group(1))))
    return rows


def test_emit_sql_matches_migration_0026():
    files = sorted(SEED_DIR.glob("phrases_*.csv")) + sorted(
        SEED_DIR.glob("snippets_*.json")
    )
    assert files, "committed corpus files missing"
    phrases, snippets = [], []
    for f in files:
        if f.suffix == ".csv":
            rows, errs = validator.check_phrases_csv(f)
            assert not errs
            phrases.extend(rows)
        else:
            rows, errs = validator.check_snippets_json(f)
            assert not errs
            snippets.extend(rows)
    generated = _rows_from_sql(validator.emit_sql(phrases, snippets))
    committed = _rows_from_sql(MIGRATION_0026.read_text("utf-8"))
    assert generated == committed


# ── DB-constraint mirror sanity ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("attr", "value"),
    [("PHRASE_MAX", 80), ("TRIGGER_MAX", 32), ("EXPANSION_MAX", 4000)],
)
def test_limits_mirror_db_checks(attr, value):
    assert getattr(validator, attr) == value
