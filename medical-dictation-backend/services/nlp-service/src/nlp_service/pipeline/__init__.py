"""Sprint-05 NLP pipeline: orchestrator + stage interface."""

from .base import (
    AbbreviationSnapshot,
    CommandSlot,
    ConfidenceSpan,
    Operation,
    PipelineWarning,
    ProcessingContext,
    Stage,
    StageInput,
    StageOutput,
    Word,
)
from .orchestrator import Orchestrator, idempotence_key

__all__ = [
    "AbbreviationSnapshot",
    "CommandSlot",
    "ConfidenceSpan",
    "Operation",
    "Orchestrator",
    "PipelineWarning",
    "ProcessingContext",
    "Stage",
    "StageInput",
    "StageOutput",
    "Word",
    "idempotence_key",
]
