#!/usr/bin/env python3
"""Pre-commit hook: reject direct asyncpg.connect / pool.acquire outside libs/db/.

Why: the only sanctioned way to obtain a tenant-scoped DB connection is
``libs/db.tenant_connection``. Direct ``asyncpg`` use bypasses the RLS
contract — see ADR-0004.

Override (rare, for non-tenant infra code): inline ``# noqa: DB001``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERN = re.compile(r"\basyncpg\.(connect|create_pool)\b")


def main(paths: list[str]) -> int:
    failed = False
    for raw in paths:
        p = Path(raw)
        if not p.is_file() or p.suffix != ".py":
            continue
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "noqa: DB001" in line:
                continue
            if PATTERN.search(line):
                print(
                    f"{p}:{lineno}: DB001 direct asyncpg use outside libs/db/ — "
                    f"use libs/db.tenant_connection()",
                    file=sys.stderr,
                )
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
