#!/usr/bin/env python3
"""Dump the OpenAPI 3.1 spec from auth-service to docs/api/.

Run via ``make openapi-dump``. The snapshot is committed; CI diffs the
live spec against the committed copy and fails on drift.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make tests-time imports work without a running server.
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

ROOT = Path(__file__).resolve().parent.parent.parent

OUT = ROOT / "docs" / "api" / "auth-service-openapi.json"


def main() -> int:
    from auth_service.main import create_app

    app = create_app()
    spec = app.openapi()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(spec.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
