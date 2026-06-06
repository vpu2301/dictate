#!/usr/bin/env python3
"""Pre-commit hook: reject ``os.environ`` reads outside ``config.py``.

Why: every envvar must flow through ``pydantic-settings`` in a service's
``config.py``. Direct reads scatter the trust boundary, make it impossible
to audit which secrets a service consumes, and bypass ``Secret[T]`` wrapping.

Override (rare): inline ``# noqa: ENV001`` on the offending line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERN = re.compile(r"\bos\.environ\b|\bos\.getenv\b")


def main(paths: list[str]) -> int:
    failed = False
    for raw in paths:
        p = Path(raw)
        if not p.is_file() or p.suffix != ".py":
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
