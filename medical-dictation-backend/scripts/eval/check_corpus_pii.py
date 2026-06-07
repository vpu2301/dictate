#!/usr/bin/env python3
"""CI gate — sweep the WER corpus for likely PII patterns.

Sprint-07: any 10-digit number (potential IPN), 7+ digit sequence near
"ІПН"/"ID"/"passport", or common Ukrainian name patterns flagged for
DPO review.

This is a coarse filter. Real anonymisation is done by the linguist +
clinical content lead at authorship; this sweep is the second line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parents[2] / "eval" / "corpus"

# 10-digit unbroken sequence → likely IPN.
IPN_RE = re.compile(r"\b\d{10}\b")
# 7-13 digit sequence near IPN-context terms.
CONTEXT_TERMS = re.compile(
    r"(іпн|id|passport|номер|серія|посвідчення)[\s:№#]*?(\d{7,13})",
    re.IGNORECASE | re.UNICODE,
)


def sweep_text(text: str, path: Path) -> list[str]:
    findings: list[str] = []
    for m in IPN_RE.finditer(text):
        findings.append(f"{path}: 10-digit (IPN?): {m.group(0)}")
    for m in CONTEXT_TERMS.finditer(text):
        findings.append(f"{path}: context+digits: term={m.group(1)} digits={m.group(2)}")
    return findings


def main() -> int:
    if not CORPUS_DIR.exists():
        print(f"warn: corpus dir {CORPUS_DIR} does not exist", file=sys.stderr)
        return 0
    failures: list[str] = []
    for path in sorted(CORPUS_DIR.rglob("transcript.txt")):
        try:
            text = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        failures.extend(sweep_text(text, path.relative_to(CORPUS_DIR.parent)))
    if failures:
        print("=== PII sweep findings ===", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        print(
            "\nReview required by DPO. Block merge until cleared.",
            file=sys.stderr,
        )
        return 1
    print("ok: no PII patterns detected in corpus transcripts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
