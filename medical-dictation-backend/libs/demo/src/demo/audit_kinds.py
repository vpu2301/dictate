"""Sprint-07 demo audit kinds.

Appended to the canonical audit kinds list in
`docs/audit/audit-kinds.md`. The set MUST stay closed — anything else
the demo emits must be added here first, otherwise the audit verifier
rejects the row.
"""

from __future__ import annotations

DEMO_AUDIT_KINDS: frozenset[str] = frozenset({
    "demo.rate_limit_hit",        # a request was rejected by the limiter
    "demo.session_capped",        # session duration exceeded MAX_SESSION_MIN
    "demo.daily_minutes_capped",  # per-user daily wall-clock minute budget exhausted
    "demo.ip_blocked",            # IP repeatedly hit caps; entered cooldown
    "demo.privacy_test_passed",   # daily release-gate (scripts/eval/run_daily_privacy_test.py)
    "demo.privacy_test_failed",   # ditto, but failed — pages DPO + security
})
