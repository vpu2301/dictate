#!/usr/bin/env python3
"""CI gate — the dev_password signing scaffold can never reach production.

Guard 3 of 3 for the S09-revision ``dev_password`` provider (guard 1 is
``DevPasswordProvider.__init__``'s environment refusal; guard 2 is the
signing-service config model rejecting the flag when
``ENVIRONMENT=production``).

This gate fails the build when any production-looking configuration
enables ``SIGNING_DEV_PASSWORD_ENABLED``:

1. Any file whose path or name suggests production (``prod``,
   ``production``, ``staging``, ``release``) that sets the flag truthy.
2. Any compose/helm/env/yaml file that sets the flag truthy alongside
   ``ENVIRONMENT`` = production/staging in the same file.

Exit codes:
    0 — no violations
    1 — violations printed to stderr
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FLAG = "SIGNING_DEV_PASSWORD_ENABLED"
TRUTHY = re.compile(
    rf"{FLAG}\s*[:=]\s*['\"]?(true|1|yes|on)['\"]?", re.IGNORECASE
)
PROD_ENV = re.compile(
    r"ENVIRONMENT\s*[:=]\s*['\"]?(production|prod|staging)['\"]?", re.IGNORECASE
)
PRODISH_PATH = re.compile(r"(prod|production|staging|release)", re.IGNORECASE)

SCAN_SUFFIXES = {".yml", ".yaml", ".env", ".toml", ".json", ".conf", ".sh", ""}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache"}
# Test trees may legitimately exercise the flag (e.g. asserting the
# config model rejects it); source/docs may mention it in prose.
SKIP_PREFIXES = ("docs/",)
SKIP_PARTS = {"tests", "test"}


def scan() -> list[str]:
    violations: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        parts = set(rel.parts)
        if parts & SKIP_DIRS or parts & SKIP_PARTS:
            continue
        rel_str = str(rel)
        if rel_str.startswith(SKIP_PREFIXES) or rel_str == "scripts/ci/check-no-dev-signing-in-prod-config.py":
            continue
        if path.suffix.lower() not in SCAN_SUFFIXES and ".env" not in path.name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not TRUTHY.search(text):
            continue
        if PRODISH_PATH.search(rel_str):
            violations.append(
                f"{rel_str}: enables {FLAG} in a production-looking config file"
            )
        elif PROD_ENV.search(text):
            violations.append(
                f"{rel_str}: enables {FLAG} alongside a production/staging ENVIRONMENT"
            )
    return violations


def main() -> int:
    violations = scan()
    if violations:
        print(
            "check-no-dev-signing-in-prod-config: the dev_password signing "
            "scaffold must NEVER be enabled in production configs:",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print("check-no-dev-signing-in-prod-config: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
