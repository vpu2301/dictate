"""Sprint-13 ICD-10 loader parsing/validation tests (no DB).

The loader script lives at ``scripts/load-icd10.py`` — loaded by path
because ``scripts/`` is not an importable package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO / "scripts" / "load-icd10.py"
_FIXTURE = _REPO / "infra" / "seeds" / "icd10" / "fixture.csv"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("load_icd10", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["load_icd10"] = module
    spec.loader.exec_module(module)
    return module


loader = _load_module()


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "codes.csv"
    path.write_text(body, "utf-8")
    return path


HEADER = "code,display_uk,display_en,parent_code,chapter,is_leaf\n"


# ── the committed fixture is the dialect contract ───────────────────


def test_committed_fixture_parses_clean() -> None:
    rows, errors = loader.parse_csv(_FIXTURE)
    assert errors == []
    assert len(rows) > 200, "fixture should cover a useful slice of МКХ-10"


def test_fixture_contains_the_hypertension_and_diabetes_families() -> None:
    rows, _ = loader.parse_csv(_FIXTURE)
    codes = {r.code for r in rows}
    assert {"I10", "I11", "I11.0", "I11.9"} <= codes
    assert {"E11", "E11.9", "E11.2"} <= codes


def test_fixture_parents_all_resolve_and_headings_are_not_leaves() -> None:
    rows, _ = loader.parse_csv(_FIXTURE)
    by_code = {r.code: r for r in rows}
    for row in rows:
        if row.parent_code is not None:
            assert row.parent_code in by_code, f"{row.code} has a dangling parent"
            assert by_code[row.parent_code].is_leaf is False, (
                f"{row.parent_code} has children so it must be a heading"
            )


# ── validation ──────────────────────────────────────────────────────


def test_bad_code_rejected_with_line_number(tmp_path: Path) -> None:
    path = _write(tmp_path, HEADER + "I10,Гіпертензія,,,IX,true\nNOPE,Погано,,,IX,true\n")
    rows, errors = loader.parse_csv(path)
    assert len(rows) == 1
    assert len(errors) == 1
    assert "line 3" in errors[0] and "NOPE" in errors[0]


def test_empty_display_uk_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, HEADER + "I10,,Hypertension,,IX,true\n")
    _, errors = loader.parse_csv(path)
    assert any("empty display_uk" in e and "line 2" in e for e in errors)


def test_duplicate_code_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, HEADER + "I10,А,,,IX,true\nI10,Б,,,IX,true\n")
    _, errors = loader.parse_csv(path)
    assert any("duplicate code" in e and "line 3" in e for e in errors)


def test_dangling_parent_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, HEADER + "I11.0,Дитина,,I11,IX,true\n")
    _, errors = loader.parse_csv(path)
    assert any("not present in the file" in e for e in errors)


def test_self_parent_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, HEADER + "I10,Гіпертензія,,I10,IX,true\n")
    _, errors = loader.parse_csv(path)
    assert any("its own parent" in e for e in errors)


def test_bad_boolean_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, HEADER + "I10,Гіпертензія,,,IX,maybe\n")
    _, errors = loader.parse_csv(path)
    assert any("is not a boolean" in e for e in errors)


def test_missing_required_column_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "code,display_en\nI10,Hypertension\n")
    _, errors = loader.parse_csv(path)
    assert any("missing required column" in e for e in errors)


def test_code_is_uppercased_and_defaults_applied(tmp_path: Path) -> None:
    path = _write(tmp_path, "code,display_uk\ni10,Гіпертензія\n")
    rows, errors = loader.parse_csv(path)
    assert errors == []
    assert rows[0].code == "I10"
    assert rows[0].is_leaf is True  # column omitted ⇒ selectable
    assert rows[0].parent_code is None
    assert rows[0].display_en == ""


@pytest.mark.parametrize("code", ["I10", "E11.9", "S06.0", "T78.4", "Z00.0"])
def test_real_dialect_codes_accepted(code: str) -> None:
    assert loader.CODE_RE.match(code)


@pytest.mark.parametrize("code", ["i10", "110", "I1", "I10.", "IX", "I10.ABCDE"])
def test_invalid_dialect_codes_rejected(code: str) -> None:
    assert not loader.CODE_RE.match(code)


# ── parent-before-child ordering ────────────────────────────────────


def test_order_parents_first_places_parents_ahead(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        HEADER
        + "I11.0,Дитина,,I11,IX,true\n"
        + "I11,Батько,,,IX,false\n"
        + "I10,Самостійний,,,IX,true\n",
    )
    rows, errors = loader.parse_csv(path)
    assert errors == []
    ordered = [r.code for r in loader.order_parents_first(rows)]
    assert ordered.index("I11") < ordered.index("I11.0")
    assert set(ordered) == {"I10", "I11", "I11.0"}


def test_order_parents_first_on_the_real_fixture() -> None:
    rows, _ = loader.parse_csv(_FIXTURE)
    ordered = loader.order_parents_first(rows)
    assert len(ordered) == len(rows)
    seen: set[str] = set()
    for row in ordered:
        if row.parent_code is not None:
            assert row.parent_code in seen, f"{row.code} came before its parent"
        seen.add(row.code)
