"""libs/asr_models — wire-stable types for ASR jobs and outputs.

These types are shared across asr-service and asr-worker (and consumed by
the NLP postprocessor in sprint 05). Pinning them in their own lib means
a schema bump is a single PR with cross-service review.
"""

from .errors import (
    ERROR_SPECS,
    UNKNOWN_SPEC,
    ErrorSpec,
    ErrorStage,
    JobErrorKind,
    is_retryable,
    spec_for,
)
from .job import JobEnqueuePayload, JobStatus, TranscriptionJobView
from .output import (
    ConfidenceSpanView,
    EnrichedSegment,
    Segment,
    TranscriptionMetadata,
    TranscriptionOutput,
    TranscriptResultView,
    WordTiming,
)

__all__ = [
    "ERROR_SPECS",
    "UNKNOWN_SPEC",
    "ConfidenceSpanView",
    "EnrichedSegment",
    "ErrorSpec",
    "ErrorStage",
    "JobEnqueuePayload",
    "JobErrorKind",
    "JobStatus",
    "Segment",
    "TranscriptionJobView",
    "TranscriptionMetadata",
    "TranscriptionOutput",
    "TranscriptResultView",
    "WordTiming",
    "is_retryable",
    "spec_for",
]
