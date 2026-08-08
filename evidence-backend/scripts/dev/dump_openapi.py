#!/usr/bin/env python3
"""Refresh docs/api/*-openapi.json from the service apps (rule E11).

Platform dump-openapi pattern: TESTING set before import so create_app()
works serverless; sorted-keys JSON for deterministic diffs.
Run via: uv run --project services/evidence-retrieval python scripts/dev/dump_openapi.py
(the retrieval project's env can import all three services' deps? No — each
service dumps under its own project; this script is invoked per-service by
the Makefile with SERVICE=<name>.)
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("EVA_GATEWAY_TESTING", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT / "docs" / "api"

SERVICES = {
    "evidence-model-gateway": (
        "evidence_model_gateway.main",
        "evidence-model-gateway-openapi.json",
    ),
    "evidence-ingest": ("evidence_ingest.main", "evidence-ingest-openapi.json"),
    "evidence-retrieval": ("evidence_retrieval.main", "evidence-retrieval-openapi.json"),
    "evidence-websearch": ("evidence_websearch.main", "evidence-websearch-openapi.json"),
    "evidence-answer": ("evidence_answer.main", "evidence-answer-openapi.json"),
}


def main() -> int:
    service = sys.argv[1] if len(sys.argv) > 1 else None
    if service not in SERVICES:
        print(f"usage: dump_openapi.py <{'|'.join(SERVICES)}>")
        return 2
    module_path, filename = SERVICES[service]
    module = importlib.import_module(module_path)
    app = module.create_app()
    spec = app.openapi()
    out = OUT_DIR / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
