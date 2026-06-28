"""Audit event kinds emitted by dictation-service. See docs/audit/event-kinds.md."""

from __future__ import annotations

from typing import Final

# Session lifecycle
SESSION_STARTED: Final = "dictation.session.started"
SESSION_RESUMED: Final = "dictation.session.resumed"
SESSION_FINALIZED: Final = "dictation.session.finalized"
SESSION_ABANDONED: Final = "dictation.session.abandoned"
SESSION_FAILED: Final = "dictation.session.failed"

# Audio
AUDIO_UPLOADED: Final = "dictation.audio.uploaded"
AUDIO_TRUNCATED: Final = "dictation.audio.truncated"  # severity=warn

# Upgrade rejections
UPGRADE_FAILED: Final = "dictation.upgrade.failed"  # warn/sec by cause

# Section-aware dictation (sprint 06)
SECTION_SWITCHED: Final = "dictation.section_switched"
