#!/usr/bin/env python3
"""CI gate: no candidate phrase text and no LLM API keys in corpus-pipeline
log calls (sprint 21 deployment plan, extending the sprint-15 secret-leak
pattern).

Candidates are PHI-derived by construction until reviewed — log ids and
counts, never content. Scope: logging/logger calls in corpus-forge and the
autocomplete-service corpus surface. Operator-facing stdout (`print`) of a
*release artifact* is deliberately out of scope: released rows are the
shipped, reviewed corpus.

Heuristic on purpose (grep-gate family): a logger call whose argument list
mentions a phrase-ish or key-ish identifier fails. False positives are
cheap to fix (rename or drop the field); false negatives are what code
review is for.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SCOPES = [
    ROOT / "services/corpus-forge/src",
    ROOT / "services/autocomplete-service/src/autocomplete_service/routers/corpus.py",
    ROOT / "services/autocomplete-service/src/autocomplete_service/corpus_repository.py",
]

# A logger call spanning one logical line that references forbidden content.
LOG_CALL = re.compile(r"\b(?:logger|logging)\.(?:debug|info|warning|error|exception|critical)\(")
FORBIDDEN = re.compile(
    r"\b(phrase|edited_text|candidate_text|prompt|api_key|external_api_key|authorization)\b",
    re.IGNORECASE,
)


def logical_call(lines: list[str], start: int) -> str:
    """The call text from its opening line until parens balance (bounded)."""
    depth = 0
    chunk: list[str] = []
    for line in lines[start : start + 12]:
        chunk.append(line)
        depth += line.count("(") - line.count(")")
        if depth <= 0 and chunk:
            break
    return "\n".join(chunk)


def scan(path: Path) -> list[str]:
    violations: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if LOG_CALL.search(line):
            call = logical_call(lines, i)
            match = FORBIDDEN.search(call)
            if match:
                violations.append(f"{path.relative_to(ROOT)}:{i + 1}: log call mentions {match.group(0)!r}")
    return violations


def main() -> int:
    violations: list[str] = []
    for scope in SCOPES:
        files = [scope] if scope.is_file() else sorted(scope.rglob("*.py"))
        for f in files:
            violations.extend(scan(f))
    if violations:
        print("corpus log hygiene: PHI-derived text / keys must not reach logs:")
        for v in violations:
            print(f"  {v}")
        return 1
    print("check-corpus-log-hygiene: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
