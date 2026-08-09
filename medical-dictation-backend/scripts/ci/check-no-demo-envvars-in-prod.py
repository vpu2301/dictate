#!/usr/bin/env python3
"""CI gate — demo/dev escape hatches can never reach production configs.

Sprint 16 pays the sprint-07 carry-over. The demo stack (HF Space) runs
with switches that would be catastrophic in production:

- ``MD_OBJECT_STORE_DISABLED`` — no audio at rest (privacy posture for
  the public demo; data loss in a clinic).
- ``MDX_DEMO_MODE`` / ``DEMO_*`` — demo rate-limit middleware & friends.
- ``AUTH_BYPASS_DEV`` — disables JWT enforcement outright.

Mirrors ``check-no-dev-signing-in-prod-config``: a violation is any
production-looking file (path contains prod/production/staging/release)
that sets one of these truthy, or any config file that sets one truthy
alongside ``ENVIRONMENT=production|staging`` in the same file.

Exit codes: 0 — clean; 1 — violations printed to stderr.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FLAGS = [
    "MD_OBJECT_STORE_DISABLED",
    "MDX_DEMO_MODE",
    "AUTH_BYPASS_DEV",
    r"DEMO_[A-Z0-9_]+",
]
TRUTHY = re.compile(
    r"(?P<flag>" + "|".join(FLAGS) + r")\s*[:=]\s*['\"]?(true|1|yes|on)['\"]?",
    re.IGNORECASE,
)
PROD_ENV = re.compile(
    r"ENVIRONMENT\s*[:=]\s*['\"]?(production|prod|staging)['\"]?", re.IGNORECASE
)
PRODISH_PATH = re.compile(r"(prod|production|staging|release)", re.IGNORECASE)

SCAN_SUFFIXES = {".yml", ".yaml", ".env", ".toml", ".json", ".conf", ".sh", ""}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache"}
SKIP_PREFIXES = ("docs/",)
SKIP_PARTS = {"tests", "test"}
SELF = "scripts/ci/check-no-demo-envvars-in-prod.py"


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
        if rel_str.startswith(SKIP_PREFIXES) or rel_str == SELF:
            continue
        if path.suffix.lower() not in SCAN_SUFFIXES and ".env" not in path.name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = TRUTHY.search(text)
        if not m:
            continue
        flag = m.group("flag")
        if PRODISH_PATH.search(rel_str):
            violations.append(
                f"{rel_str}: enables {flag} in a production-looking config file"
            )
        elif PROD_ENV.search(text):
            violations.append(
                f"{rel_str}: enables {flag} alongside a production/staging ENVIRONMENT"
            )
    return violations


def main() -> int:
    violations = scan()
    if violations:
        print(
            "check-no-demo-envvars-in-prod: demo/dev escape hatches "
            "(MD_OBJECT_STORE_DISABLED, MDX_DEMO_MODE, DEMO_*, AUTH_BYPASS_DEV) "
            "must NEVER be enabled in production configs:",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print("check-no-demo-envvars-in-prod: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
