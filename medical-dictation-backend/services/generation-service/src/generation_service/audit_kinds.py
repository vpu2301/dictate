"""Audit event kinds emitted by generation-service (sprint 15).

Documented in docs/audit/event-kinds.md; written via libs/audit.AuditWriter.
"""

from __future__ import annotations

from typing import Final

# Aggregated (one row per tenant per flush interval, payload {"count": n}) —
# per-keystroke rows would pollute the hash chain.
LAYER_C_COMPLETION_SHOWN: Final = "layer_c.completion.shown"
# Warn, immediate: the output safety filter dropped a completion that tried
# to introduce a clinical value absent from the typed text.
LAYER_C_COMPLETION_FILTERED: Final = "layer_c.completion.filtered"
