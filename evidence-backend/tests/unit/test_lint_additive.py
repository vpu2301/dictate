"""S01 verification protocol §9.5: additive linter proven both directions (unit level).

The scratch-branch CI proof is in the verification protocol; these tests pin
the classification logic itself.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_SPEC_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "contracts" / "lint_additive.py"
)
_spec = importlib.util.spec_from_file_location("lint_additive", _SPEC_PATH)
assert _spec and _spec.loader
lint_additive = importlib.util.module_from_spec(_spec)
sys.modules["lint_additive"] = lint_additive
_spec.loader.exec_module(lint_additive)


def _schema(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "kind": {"type": "string", "enum": ["evidence", "interpretation"]},
            "note": {"type": "string"},
        },
        "required": ["id", "kind"],
        "$defs": {
            "Citation": {
                "type": "object",
                "properties": {"source_id": {"type": "string"}},
                "required": ["source_id"],
            }
        },
        "x-contract-version": "1.0",
    }
    base.update(overrides)
    return base


def _lint_one(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    return lint_additive.lint({"seg.v1.schema.json": old}, {"seg.v1.schema.json": new})


class TestBreakingDetected:
    def test_removed_property_fails(self) -> None:
        new = _schema()
        del new["properties"]["note"]
        assert any("removed" in p for p in _lint_one(_schema(), new))

    def test_removed_enum_value_fails(self) -> None:
        new = _schema()
        new["properties"]["kind"]["enum"] = ["evidence"]
        assert any("enum values removed" in p for p in _lint_one(_schema(), new))

    def test_type_change_fails(self) -> None:
        new = _schema()
        new["properties"]["note"] = {"type": "integer"}
        assert any("type changed" in p for p in _lint_one(_schema(), new))

    def test_newly_required_existing_property_fails(self) -> None:
        new = _schema()
        new["required"] = ["id", "kind", "note"]
        assert any("became required" in p for p in _lint_one(_schema(), new))

    def test_removed_defs_entry_fails(self) -> None:
        new = _schema()
        new["$defs"] = {}
        assert any("$defs entry" in p for p in _lint_one(_schema(), new))

    def test_removed_contract_file_fails(self) -> None:
        problems = lint_additive.lint({"seg.v1.schema.json": _schema()}, {})
        assert any("contract removed" in p for p in problems)

    def test_version_downgrade_fails(self) -> None:
        new = _schema()
        new["x-contract-version"] = "0.9"
        assert any("lowered" in p for p in _lint_one(_schema(), new))


class TestAdditiveAllowed:
    def test_identical_schemas_pass(self) -> None:
        assert _lint_one(_schema(), _schema()) == []

    def test_new_optional_property_passes(self) -> None:
        new = _schema()
        new["properties"]["extra"] = {"type": "string"}
        assert _lint_one(_schema(), new) == []

    def test_new_enum_value_passes(self) -> None:
        new = _schema()
        new["properties"]["kind"]["enum"] = ["evidence", "interpretation", "uncertainty"]
        assert _lint_one(_schema(), new) == []

    def test_new_contract_file_passes(self) -> None:
        problems = lint_additive.lint(
            {"seg.v1.schema.json": _schema()},
            {"seg.v1.schema.json": _schema(), "new.v1.schema.json": _schema()},
        )
        assert problems == []

    def test_breaking_change_with_major_bump_passes(self) -> None:
        new = _schema()
        del new["properties"]["note"]
        new["x-contract-version"] = "2.0"
        assert _lint_one(_schema(), new) == []
