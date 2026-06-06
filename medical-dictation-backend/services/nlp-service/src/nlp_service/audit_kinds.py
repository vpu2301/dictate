"""Audit event kinds emitted by nlp-service. See docs/audit/event-kinds.md."""

from __future__ import annotations

from typing import Final

# Frontend emits these via POST /audit/events/voice_command_*; nlp-service
# never emits voice_command.* itself (cardinality concern + the frontend
# knows the actual outcome).
VOICE_COMMAND_EXECUTED: Final = "voice_command.executed"
VOICE_COMMAND_UNDONE: Final = "voice_command.undone"
VOICE_COMMAND_EXECUTED_FAILED: Final = "voice_command.executed_failed"

# Admin actions on the abbreviation dictionary.
ABBREVIATION_POLICY_SET: Final = "abbreviation.policy.set"
ABBREVIATION_POLICY_DELETED: Final = "abbreviation.policy.deleted"

# Dictation-service emits this when our 200-ms call times out and it falls
# back to raw Whisper text. Listed here because it's about NLP.
DICTATION_NLP_TIMEOUT: Final = "dictation.nlp_timeout"
