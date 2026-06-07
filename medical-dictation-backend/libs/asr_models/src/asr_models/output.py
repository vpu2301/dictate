"""The transcript JSON schema.

Persisted as encrypted JSON in the transcripts bucket. The worker writes
it; the API returns a pre-signed URL to it; sprint 05's NLP postprocessor
consumes it. Stable by contract — additions are fine, breaking changes
need a wire-version bump and a migration story.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, NonNegativeFloat, NonNegativeInt


class WordTiming(BaseModel):
    text: str
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    probability: float = Field(ge=0.0, le=1.0)


class Segment(BaseModel):
    text: str
    start_ms: NonNegativeInt
    end_ms: NonNegativeInt
    words: list[WordTiming] = Field(default_factory=list)
    avg_confidence: float = Field(ge=0.0, le=1.0)


class TranscriptionMetadata(BaseModel):
    model: str
    prompt_id: UUID | None = None
    vad_seconds_speech: NonNegativeFloat
    infer_seconds: NonNegativeFloat
    gpu_seconds: NonNegativeFloat = 0.0
    peak_gpu_mem_mb: NonNegativeInt = 0
    beam_size: int = Field(ge=1)


class TranscriptionOutput(BaseModel):
    language: str = Field(pattern=r"^(uk|en)$")
    segments: list[Segment]
    metadata: TranscriptionMetadata
    schema_version: int = 1
