#!/usr/bin/env python3
"""CI gate — direct ``boto3``/``aioboto3``/``minio`` imports must live in libs/storage.

Sprint 03 introduces ``libs/storage.EncryptedObjectStore`` as the only
sanctioned write/read path for tenant-bearing object data. Bypassing it
risks plaintext-at-rest. This script greps the repo and rejects any
direct import outside ``libs/storage``.

Run as part of ``make ci``:

    python scripts/ci/check-no-direct-object-storage.py

Exit codes:
    0 — no violations
    1 — violations printed to stderr
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BANNED_IMPORTS = re.compile(
    r"^\s*(?:import|from)\s+(?:boto3|aioboto3|minio)\b",
    re.MULTILINE,
)

ALLOWED_PREFIXES = (
    "libs/storage/",
    "scripts/dev/",
    "scripts/ci/check-no-direct-object-storage.py",
)


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    violations: list[tuple[Path, int, str]] = []

    for path in repo.rglob("*.py"):
        rel = path.relative_to(repo).as_posix()
        if any(rel.startswith(p) for p in ALLOWED_PREFIXES):
            continue
        if (
            "/.venv/" in rel
            or "/__pycache__/" in rel
            or "site-packages" in rel
            or rel.startswith(".venv/")
            or rel.startswith("dist/")
        ):
            continue
        try:
            text = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in BANNED_IMPORTS.finditer(text):
            line_no = text[: match.start()].count("\n") + 1
            violations.append((path, line_no, match.group(0).strip()))

    if violations:
        print(
            "ERROR: direct boto3/aioboto3/minio imports outside libs/storage:",
            file=sys.stderr,
        )
        for path, line, snippet in violations:
            print(f"  {path.relative_to(repo)}:{line}  {snippet}", file=sys.stderr)
        print(
            "\nUse libs/storage.EncryptedObjectStore instead. PHI bytes must "
            "never be written to MinIO/S3 outside the envelope path.",
            file=sys.stderr,
        )
        return 1
    print("ok: no direct object-storage imports outside libs/storage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
