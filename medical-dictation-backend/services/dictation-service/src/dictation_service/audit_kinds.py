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

# NLP degradation (documented since sprint 05; constant added when the
# finalize-time NLP pass was actually wired — sprint 14).
NLP_TIMEOUT: Final = "dictation.nlp_timeout"  # severity=warn

# Conversation mode (sprint 14). SESSION_STARTED gains `mode` in its
# payload; these three are conversation-specific.
SPEAKER_MAPPING_INFERRED: Final = "conversation.speaker_mapping.inferred"
SPEAKER_MAPPING_MANUAL_SET: Final = "conversation.speaker_mapping.manual_set"
CONSENT_REFUSED: Final = "conversation.consent_refused"  # severity=warn

# Draft creation on conversation finalize (via report-service POST /v1/reports)
DRAFT_CREATED: Final = "conversation.draft.created"
DRAFT_CREATE_FAILED: Final = "conversation.draft.create_failed"  # severity=warn
