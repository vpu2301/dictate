#!/usr/bin/env python3
"""Gate E12: model access ONLY via libs/models (the gateway client).

Violations anywhere under services/ except evidence-model-gateway itself:
  - importing a model runtime (sentence_transformers, torch, vllm, transformers)
  - hardcoding gateway endpoints ("/v1/embed", "/v1/generate") — services must
    call ModelGatewayClient, never httpx-POST the gateway by hand.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SERVICES = ROOT / "services"
EXEMPT_SERVICE = "evidence-model-gateway"
FORBIDDEN_IMPORTS = {"sentence_transformers", "torch", "vllm", "transformers", "onnxruntime"}
FORBIDDEN_LITERALS = ("/v1/embed", "/v1/generate")


def check_file(path: Path) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if name.split(".")[0] in FORBIDDEN_IMPORTS:
                violations.append(f"{path}:{node.lineno}: forbidden model-runtime import {name!r}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for literal in FORBIDDEN_LITERALS:
                if literal in node.value:
                    violations.append(
                        f"{path}:{node.lineno}: hardcoded gateway endpoint {literal!r} "
                        "(use models.ModelGatewayClient)"
                    )
    return violations


def main() -> int:
    violations: list[str] = []
    for path in sorted(SERVICES.rglob("*.py")):
        if EXEMPT_SERVICE in path.parts or "tests" in path.parts:
            continue
        violations.extend(check_file(path))
    if violations:
        print("check-no-direct-model: E12 violations (model access only via libs/models):")
        for v in violations:
            print(f"  {v}")
        return 1
    print("check-no-direct-model: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
