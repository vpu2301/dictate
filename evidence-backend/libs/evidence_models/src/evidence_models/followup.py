"""FollowUp v1 (rule RC3, logic rules FU1-FU3)."""

from __future__ import annotations

from enum import StrEnum

from .common import StrictModel


class FollowUpPriority(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class FollowUpAnswerType(StrEnum):
    boolean = "boolean"
    choice = "choice"
    number = "number"
    quantity = "quantity"
    text = "text"


class FollowUp(StrictModel):
    id: str
    question: str
    why_needed: str
    priority: FollowUpPriority
    answer_type: FollowUpAnswerType
    choices: list[str] | None = None
    unit: str | None = None
    # Only curated, clinically signed follow-ups may block an answer (rule FU3).
    blocking: bool = False
