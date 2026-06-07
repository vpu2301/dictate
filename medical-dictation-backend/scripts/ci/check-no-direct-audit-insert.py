#!/usr/bin/env python3
"""CI gate: ``INSERT INTO audit.events`` must only happen inside libs/audit.

Spec § 8 risk E2: a developer might bypass ``AuditWriter`` by inserting
directly. The audit_writer Postgres role gates this at runtime, but a
service-account misconfiguration could re-grant INSERT to app_role.
This grep is the belt to the role-grant's braces.

Allowed locations:
  - libs/audit/**         (the writer itself)
  - infra/postgres/**     (migrations)
  - scripts/**            (DB tooling, e.g. nightly-verify)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ALLOWED_PREFIXES: tuple[str, ...] = (
    "libs/audit/",
    "infra/postgres/",
    "scripts/",
)
SCANNED_EXTS: tuple[str, ...] = (".py", ".sql")
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {".venv", "__pycache__", "node_modules", ".git", "dist", "build"}
)

# Match `INSERT INTO audit.events` and `INSERT INTO "audit"."events"` etc.
# Also catch ``COPY audit.events FROM …`` (another way to write rows).
PATTERN = re.compile(
    r"(?i)(insert\s+into|copy)\s+(?:\"?audit\"?\.)?\"?events\"?",
    re.MULTILINE,
)


def main() -> int:
    failures: list[str] = []
    scanned = 0
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in SCANNED_EXTS:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if rel == "scripts/ci/check-no-direct-audit-insert.py":
            continue  # this very file contains the pattern
        if any(rel.startswith(p) for p in ALLOWED_PREFIXES):
            continue
        scanned += 1
        try:
            text = path.read_text()
        except (UnicodeDecodeError, PermissionError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            # Only match writes to the audit schema specifically — many
            # tables have an `events` column or table elsewhere.
            if re.search(
                r"audit\.events|audit\"\.\"events", line, re.IGNORECASE
            ) and PATTERN.search(line):
                failures.append(f"{rel}:{lineno}: {line.strip()[:120]}")

    if failures:
        print(
            "FAIL: direct audit.events writes detected outside libs/audit:",
            file=sys.stderr,
        )
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nWrite audit events via `from audit import AuditWriter` and the "
            "writer's audit_writer-credentialed pool.",
            file=sys.stderr,
        )
        return 1

    print(f"PASS: scanned {scanned} file(s); no direct audit.events writes outside libs/audit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
