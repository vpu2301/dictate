#!/usr/bin/env python3
"""Gate QS1 (layer 1b): patient-snapshot types are unnameable in the web path.

The import-linter contract in `pyproject.toml` forbids a *module* dependency
on `evidence_models.snapshot`. It cannot catch the more likely mistake —

    from evidence_models import PatientSnapshot     # package re-export

— because that is an import of `evidence_models`, which every module here
does legitimately. This gate closes that hole at the symbol level: inside the
guarded paths, the snapshot type names may not appear at all, imported or
merely referenced.

Guarded paths are the code that can put bytes on the wire to a third party,
plus the planner that decides whether the web is in scope at all. `S05` will
add patient context to the *answer* service; that is expected and allowed —
what must never happen is patient context reaching `evidence_websearch` or
the module that authorizes a web query.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

GUARDED_PATHS = (
    ROOT / "services" / "evidence-websearch" / "src",
    ROOT
    / "services"
    / "evidence-answer"
    / "src"
    / "evidence_answer"
    / "domain"
    / "stages"
    / "plan.py",
)

# Everything `evidence_models.snapshot` exports, plus the module itself.
FORBIDDEN_NAMES = frozenset(
    {
        "PatientSnapshot",
        "PatientFact",
        "FactProvenance",
        "FactSource",
        "FactStatus",
        "FactOriginKind",
    }
)
FORBIDDEN_MODULES = frozenset({"evidence_models.snapshot"})


def _files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(path.rglob("*.py"))


def check_file(path: Path) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in FORBIDDEN_MODULES:
                violations.append(f"{path}:{node.lineno}: imports {node.module!r} (QS1)")
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    violations.append(
                        f"{path}:{node.lineno}: imports snapshot type "
                        f"{alias.name!r} from {node.module!r} (QS1)"
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES:
                    violations.append(f"{path}:{node.lineno}: imports {alias.name!r} (QS1)")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            violations.append(f"{path}:{node.lineno}: references snapshot type {node.id!r} (QS1)")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            violations.append(
                f"{path}:{node.lineno}: references snapshot type '…{node.attr}' (QS1)"
            )
    return violations


def main() -> int:
    violations: list[str] = []
    scanned = 0
    for guarded in GUARDED_PATHS:
        if not guarded.exists():
            print(f"check-qs1: guarded path missing: {guarded}", file=sys.stderr)
            return 1
        for path in _files(guarded):
            scanned += 1
            violations.extend(check_file(path))
    if violations:
        print("check-qs1: patient data must never reach the web path (rule QS1):")
        for violation in violations:
            print(f"  {violation}")
        return 1
    print(f"check-qs1: OK ({scanned} files; no snapshot types nameable in the web path)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
