"""Audit event kinds emitted by asr-worker."""

from __future__ import annotations

from typing import Final

TRANSCRIPTION_STARTED: Final = "asr.transcription_started"
TRANSCRIPTION_COMPLETE: Final = "asr.transcription_complete"
TRANSCRIPTION_FAILED: Final = "asr.transcription_failed"
JOB_CANCELLED: Final = "asr.job_cancelled"
