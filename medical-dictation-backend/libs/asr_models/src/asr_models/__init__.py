"""libs/asr_models — wire-stable types for ASR jobs and outputs.

These types are shared across asr-service and asr-worker (and consumed by
the NLP postprocessor in sprint 05). Pinning them in their own lib means
a schema bump is a single PR with cross-service review.
"""

from .job import JobEnqueuePayload, JobStatus, TranscriptionJobView
from .output import (
    Segment,
    TranscriptionMetadata,
    TranscriptionOutput,
    WordTiming,
)

__all__ = [
    "JobEnqueuePayload",
    "JobStatus",
    "Segment",
    "TranscriptionJobView",
    "TranscriptionMetadata",
    "TranscriptionOutput",
    "WordTiming",
]
