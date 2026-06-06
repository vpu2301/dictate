#!/usr/bin/env python3
"""CI gate — primitives from ``cryptography`` must live in libs/crypto.

Sprint 03 introduces ``libs/crypto`` as the only sanctioned envelope
path. Direct use of ``cryptography.hazmat.primitives`` elsewhere in the
codebase is a red flag — it usually means an engineer has reinvented
encryption or bypassed the envelope.

Allow-listed paths: libs/crypto/, tests/ (so adversarial tests can
construct deliberately-bad ciphertext to verify the envelope rejects it).

Exit codes:
    0 — no violations
    1 — violations printed to stderr
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BANNED_IMPORTS = re.compile(
    r"^\s*(?:import|from)\s+cryptography\.hazmat\b",
    re.MULTILINE,
)

ALLOWED_PREFIXES = (
    "libs/crypto/",
    "libs/storage/tests/",     # tampering tests
    "libs/crypto/tests/",      # adversarial tests
    "libs/auth/tests/",        # JWT signing-key fixtures (RS256 test keys)
    "scripts/ci/check-no-direct-crypto.py",
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
        print("ERROR: direct cryptography.hazmat imports outside libs/crypto:", file=sys.stderr)
        for path, line, snippet in violations:
            print(f"  {path.relative_to(repo)}:{line}  {snippet}", file=sys.stderr)
        print(
            "\nUse libs/crypto.Envelope. Any new primitive belongs in libs/crypto.",
            file=sys.stderr,
        )
        return 1
    print("ok: no direct cryptography.hazmat imports outside libs/crypto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
