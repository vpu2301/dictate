#!/usr/bin/env python3
"""Dump the OpenAPI 3.1 specs to docs/api/.

Run via ``make openapi-dump``. The snapshots are committed; CI
(``make openapi-check``) diffs the live spec against the committed copy and
fails on drift, so a public-API change must land with a refreshed snapshot.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

# Make import-time app construction work without a running server.
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT / "docs" / "api"

# (module exposing ``create_app``, output snapshot filename).
SERVICES: list[tuple[str, str]] = [
    ("auth_service.main", "auth-service-openapi.json"),
    ("asr_service.main", "asr-service-openapi.json"),
    ("report_service.main", "report-service-openapi.json"),
    ("signing_service.main", "signing-service-openapi.json"),
]


def _dump(module_path: str, filename: str) -> None:
    module = importlib.import_module(module_path)
    app = module.create_app()
    spec = app.openapi()
    out = OUT_DIR / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out.relative_to(ROOT)} ({len(spec.get('paths', {}))} paths)")


def main() -> int:
    for module_path, filename in SERVICES:
        _dump(module_path, filename)
    return 0


if __name__ == "__main__":
    sys.exit(main())
