"""Audit event kinds emitted by asr-worker."""

from __future__ import annotations

from typing import Final

TRANSCRIPTION_STARTED: Final = "asr.transcription_started"
TRANSCRIPTION_COMPLETE: Final = "asr.transcription_complete"
TRANSCRIPTION_FAILED: Final = "asr.transcription_failed"
JOB_CANCELLED: Final = "asr.job_cancelled"

# Security / startup. Emitted as a CRITICAL structured log (not a tenant-scoped
# audit row): a missing master key is a system-wide, pre-tenant fail-closed
# condition with no tenant context, so it cannot enter the per-tenant audit
# chain. See docs/audit/event-kinds.md and docs/runbooks/asr-worker.md.
KEY_MASTER_MISSING: Final = "asr.key.master_missing"  # severity=error
