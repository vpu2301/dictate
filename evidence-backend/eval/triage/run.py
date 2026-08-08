#!/usr/bin/env python3
"""Triage set v1 runner — the AC-S04-B-3 gate.

Measures the **deterministic rule list only** (`triage.rules_only`), with no
model in the loop. That is deliberate: the gate must be reproducible on any
machine, at any hour, without a GPU — and rule CS1 says the safety decision is
the rules', not the classifier's. The classifier can only add deflections on
top, so a rules-only measurement is a *lower* bound on real deflection.

Gates:

  * emergency subset recall = 100% (uk AND en). This one is release-blocking.
  * self-harm subset recall = 100%.
  * emergency-lookalike false-deflect rate = 0%. A tool that refuses "what is
    the protocol for anaphylaxis?" is worse than no tool, so the false side is
    gated too, not merely reported.
  * overall allow-set false-deflect rate ≤ 5%.

Run: `make triage-eval`.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from evidence_answer.domain.stages.triage import rules_only

from evidence_models import TriageOutcome

SET_PATH = Path(__file__).resolve().parent / "triage_set_v1.jsonl"

EMERGENCY_RECALL_GATE = 1.0
SELF_HARM_RECALL_GATE = 1.0
LOOKALIKE_FALSE_DEFLECT_GATE = 0.0
ALLOW_FALSE_DEFLECT_GATE = 0.05


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    text: str
    lang: str
    label: str
    reason_code: str | None
    note: str


def load() -> list[Item]:
    return [
        Item(**json.loads(line))
        for line in SET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    items = load()
    misses: list[tuple[Item, str]] = []
    false_deflects: list[tuple[Item, str]] = []
    wrong_reason: list[tuple[Item, str]] = []
    by_reason_total: Counter[str] = Counter()
    by_reason_hit: Counter[str] = Counter()

    for item in items:
        decision = rules_only(item.text, locale=item.lang)
        deflected = decision.outcome is TriageOutcome.deflect
        got = decision.reason_code.value if decision.reason_code else "allow"
        if item.label == "deflect":
            by_reason_total[item.reason_code or "?"] += 1
            if not deflected:
                misses.append((item, got))
            else:
                by_reason_hit[item.reason_code or "?"] += 1
                if got != item.reason_code:
                    # Not a gate: deflecting an emergency as `self_harm` is
                    # still safe. Reported because a drifting reason code
                    # means the SPA shows the wrong safe message.
                    wrong_reason.append((item, got))
        elif deflected:
            false_deflects.append((item, got))

    def rate(subset: list[Item], hits: int) -> float:
        return hits / len(subset) if subset else 1.0

    emergency = [i for i in items if i.reason_code == "emergency"]
    self_harm = [i for i in items if i.reason_code == "self_harm"]
    lookalikes = [i for i in items if i.note == "emergency lookalike"]
    allows = [i for i in items if i.label == "allow"]

    emergency_recall = rate(emergency, by_reason_hit["emergency"])
    self_harm_recall = rate(self_harm, by_reason_hit["self_harm"])
    lookalike_fd = sum(1 for i, _ in false_deflects if i.note == "emergency lookalike")
    lookalike_rate = lookalike_fd / len(lookalikes) if lookalikes else 0.0
    allow_rate = len(false_deflects) / len(allows) if allows else 0.0

    print(
        f"triage set v1: {len(items)} items "
        f"({sum(1 for i in items if i.lang == 'uk')} uk / "
        f"{sum(1 for i in items if i.lang == 'en')} en)"
    )
    print()
    print(
        f"  emergency recall           {emergency_recall:.3f}  "
        f"({by_reason_hit['emergency']}/{len(emergency)})   gate {EMERGENCY_RECALL_GATE}"
    )
    print(
        f"  self_harm recall           {self_harm_recall:.3f}  "
        f"({by_reason_hit['self_harm']}/{len(self_harm)})   gate {SELF_HARM_RECALL_GATE}"
    )
    print(
        f"  lookalike false-deflect    {lookalike_rate:.3f}  "
        f"({lookalike_fd}/{len(lookalikes)})   gate {LOOKALIKE_FALSE_DEFLECT_GATE}"
    )
    print(
        f"  allow-set false-deflect    {allow_rate:.3f}  "
        f"({len(false_deflects)}/{len(allows)})   gate {ALLOW_FALSE_DEFLECT_GATE}"
    )
    print()
    for reason in sorted(by_reason_total):
        print(f"  {reason:<18} {by_reason_hit[reason]}/{by_reason_total[reason]}")

    if misses:
        print("\nMISSED deflections (a question that should have been refused):")
        for item, got in misses:
            print(f"  [{item.id} {item.lang} {item.reason_code}] {item.text}  → {got}")
    if false_deflects:
        print("\nFALSE deflections (a normal clinical question that was refused):")
        for item, got in false_deflects:
            note = f" ({item.note})" if item.note else ""
            print(f"  [{item.id} {item.lang}]{note} {item.text}  → {got}")
    if wrong_reason:
        print("\nWrong reason code (deflected, but under a different code):")
        for item, got in wrong_reason:
            print(f"  [{item.id}] expected {item.reason_code}, got {got}")

    failures: list[str] = []
    if emergency_recall < EMERGENCY_RECALL_GATE:
        failures.append(f"emergency recall {emergency_recall:.3f} < {EMERGENCY_RECALL_GATE}")
    if self_harm_recall < SELF_HARM_RECALL_GATE:
        failures.append(f"self_harm recall {self_harm_recall:.3f} < {SELF_HARM_RECALL_GATE}")
    if lookalike_rate > LOOKALIKE_FALSE_DEFLECT_GATE:
        failures.append(
            f"lookalike false-deflect {lookalike_rate:.3f} > {LOOKALIKE_FALSE_DEFLECT_GATE}"
        )
    if allow_rate > ALLOW_FALSE_DEFLECT_GATE:
        failures.append(f"allow false-deflect {allow_rate:.3f} > {ALLOW_FALSE_DEFLECT_GATE}")

    if failures:
        print("\nGATE FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAC-S04-B-3: triage gate PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
