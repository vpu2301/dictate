#!/usr/bin/env python3
"""Gate E8: os.environ / os.getenv only inside config.py (and tests/scripts)."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCAN_DIRS = [ROOT / "services", ROOT / "libs"]


def check_file(path: Path) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in ("environ", "getenv")
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ):
            violations.append(f"{path}:{node.lineno}: os.{node.attr} outside config.py")
    return violations


def main() -> int:
    violations: list[str] = []
    for scan_dir in SCAN_DIRS:
        for path in sorted(scan_dir.rglob("*.py")):
            if path.name == "config.py" or "tests" in path.parts:
                continue
            violations.extend(check_file(path))
    if violations:
        print("check-no-os-environ: E8 violations (env reads only in config.py):")
        for v in violations:
            print(f"  {v}")
        return 1
    print("check-no-os-environ: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
