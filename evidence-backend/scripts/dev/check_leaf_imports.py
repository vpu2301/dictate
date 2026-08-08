#!/usr/bin/env python3
"""Gate: the contract/safety leaf packages import nothing internal (rule E2).

`evidence_models` may import only the stdlib, pydantic, and itself.
`evidence_safety` is stricter still: stdlib only. Any other import —
platform libs, FastAPI, DB drivers, HTTP clients — fails this gate, which
runs in `make ci`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

LIBS = Path(__file__).resolve().parent.parent.parent / "libs"

# package src dir -> extra non-stdlib top-level imports it may use
LEAF_PACKAGES: dict[Path, set[str]] = {
    LIBS / "evidence_models" / "src": {"pydantic", "evidence_models"},
    LIBS / "evidence_safety" / "src": {"evidence_safety"},
}


def violations_in(path: Path, allowed: set[str]) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:  # relative import stays inside the package
                continue
            names = [node.module] if node.module else []
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top in sys.stdlib_module_names or top in allowed:
                continue
            found.append(f"{path}:{node.lineno}: forbidden import {name!r}")
    return found


def main() -> int:
    all_violations: list[str] = []
    for src, allowed in LEAF_PACKAGES.items():
        for path in sorted(src.rglob("*.py")):
            all_violations.extend(violations_in(path, allowed))
    if all_violations:
        print("leaf packages must not import outward; forbidden imports found:")
        for line in all_violations:
            print(f"  {line}")
        return 1
    print("check_leaf_imports: OK (evidence_models = stdlib + pydantic, evidence_safety = stdlib)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
