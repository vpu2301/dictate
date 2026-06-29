"""Job-level types: the queue payload and the API view."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobEnqueuePayload(BaseModel):
    """Wire payload the API enqueues onto Redis Streams.

    The worker validates this on read; any field mismatch indicates a
    cross-version skew (asr-service deployed before asr-worker).
    """

    job_id: UUID
    tenant_id: UUID
    audio_id: UUID
    prompt_id: UUID
    language: str = Field(pattern=r"^(uk|en)$")
    model: str = "large-v3"
    requester_sub: UUID
    schema_version: int = 1


class TranscriptionJobView(BaseModel):
    """Public view of a transcription job. Returned by the GET endpoints."""

    id: UUID
    tenant_id: UUID
    audio_id: UUID
    requester_sub: UUID
    prompt_id: UUID
    language: str
    model: str
    status: JobStatus
    error_kind: str | None = None
    error_detail: str | None = None
    result_url: str | None = None  # populated only when status == complete
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempts: int = 0


class JobResultView(BaseModel):
    """Pre-signed result fetch for a COMPLETE job.

    Returned by ``GET /asr/jobs/{id}/result``. The dedicated endpoint exists so
    a client can fetch the (short-TTL) URL without polling the whole job view,
    and so "not ready yet" is an explicit 409 rather than a 200 with a null URL.
    """

    job_id: UUID
    presigned_url: str
    expires_in: int  # seconds until the URL expires (matches the TTL on issue)
