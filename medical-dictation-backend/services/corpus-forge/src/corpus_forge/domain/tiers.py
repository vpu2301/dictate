"""Deterministic tier router (ADR-0043 §6).

| Tier | Condition | Path |
| 1 | mined, all validators pass, no risk flags | jury unanimous → auto-accept (post-calibration), 5% spot-audit |
| 2 | generated / terminology / telemetry-derived, no risk flags | jury majority → accept; split → tier 3, 10% spot-audit |
| 3 | ANY risk flag | human decision mandatory — no exceptions |

Exhaustively tested in tests/unit/test_tiers.py.
"""

from __future__ import annotations

from collections.abc import Sequence

SOURCE_KINDS = ("mined", "telemetry_gap", "terminology", "generated", "authored")

TIER_AUTO = 1
TIER_MACHINE_REVIEWED = 2
TIER_HUMAN_MANDATORY = 3


def route_tier(
    *,
    source_kind: str,
    risk_flags: Sequence[str],
    validators_passed: bool,
) -> int:
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source_kind: {source_kind!r}")
    if risk_flags:
        return TIER_HUMAN_MANDATORY
    if source_kind == "mined" and validators_passed:
        return TIER_AUTO
    return TIER_MACHINE_REVIEWED
