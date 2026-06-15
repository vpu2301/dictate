#!/usr/bin/env python3
"""Pre-commit / CI gate: reject ``os.environ`` reads outside ``config.py``.

Why: every envvar must flow through ``pydantic-settings`` in a service's
``config.py``. Direct reads scatter the trust boundary, make it impossible
to audit which secrets a service consumes, and bypass ``Secret[T]`` wrapping.

The gate is self-contained: it applies its own path exclusions
(``**/config.py``, ``tests/``, ``libs/secret/``, ``scripts/``) so it behaves
identically whether pre-commit feeds it a staged file list or ``make`` feeds it
``git ls-files``. The pre-commit ``exclude:`` regex mirrors these for speed.

Override (rare): inline ``# noqa: ENV001`` on the offending line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERN = re.compile(r"\bos\.environ\b|\bos\.getenv\b")


def is_excluded(path: Path) -> bool:
    """True if ``path`` is a sanctioned env surface and must not be scanned.

    Mirrors the pre-commit ``exclude:`` regex but works on absolute paths too
    (so the unit tests can point it at tmp files). ``config.py`` is the typed
    env boundary; ``tests/`` set env to exercise it; ``libs/secret/`` is the
    ``Secret[T]`` wrapper itself; ``scripts/`` are operational tooling.
    """
    parts = path.parts
    if path.name == "config.py":
        return True
    if "tests" in parts:
        return True
    posix = path.as_posix()
    if posix.startswith("libs/secret/") or "/libs/secret/" in posix:
        return True
    return "scripts" in parts


def main(paths: list[str]) -> int:
    failed = False
    for raw in paths:
        p = Path(raw)
        if not p.is_file() or p.suffix != ".py":
            continue
        if is_excluded(p):
            continue
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "noqa: ENV001" in line:
                continue
            if PATTERN.search(line):
                print(
                    f"{p}:{lineno}: ENV001 os.environ / os.getenv outside config.py — "
                    f"use pydantic-settings",
                    file=sys.stderr,
                )
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
