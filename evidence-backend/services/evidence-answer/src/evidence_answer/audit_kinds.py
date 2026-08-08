"""Audit kinds emitted by evidence-answer (catalogued in the platform's
docs/audit/event-kinds.md, EVA-S04 section)."""

from typing import Final

ANSWER_GENERATED: Final = "evidence.answer_generated"
QUESTION_DEFLECTED: Final = "evidence.question_deflected"
