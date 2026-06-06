"""ASR-related audit event kinds. See docs/audit/event-kinds.md."""

from __future__ import annotations

from typing import Final

# Audio lifecycle
AUDIO_UPLOADED: Final = "asr.audio_uploaded"
AUDIO_DELETED: Final = "asr.audio_deleted"

# Job lifecycle
JOB_QUEUED: Final = "asr.job_queued"
TRANSCRIPTION_STARTED: Final = "asr.transcription_started"
TRANSCRIPTION_COMPLETE: Final = "asr.transcription_complete"
TRANSCRIPTION_FAILED: Final = "asr.transcription_failed"
JOB_CANCELLED: Final = "asr.job_cancelled"

# Quota
QUOTA_EXCEEDED: Final = "asr.quota_exceeded"  # severity=warn
