"""The batch-ASR failure vocabulary — one closed set, shared by both sides.

``transcription_jobs.error_kind`` is a bare ``TEXT`` column and every
producer of it lived in a different file, so "closed vocabulary" was a
docstring promise nothing enforced: the worker wrote three kinds, the
notification fan-out documented three, the API re-exported whatever the
column held, and a client had no way to tell a transient infrastructure
blip from an upload it should never send again.

This module is that vocabulary. Each kind carries the three facts a
caller actually needs:

``stage``
    Where in the job's life it died. Answers *why* it can happen: a
    ``decode`` failure is about the bytes, an ``inference`` failure is
    about the GPU, a ``lifecycle`` failure is about the fleet.
``retryable``
    Whether re-running the SAME job could plausibly succeed. Drives the
    worker's ack/DLQ decision — a corrupt file will be just as corrupt on
    the fourth delivery, and re-running it three more times only delays
    the failure the clinician is waiting for.
``resubmittable``
    Whether the person who uploaded can do something about it. ``true``
    for "fix the file and send it again"; ``false`` when the fix is an
    operator's, and telling the clinician to try again would be a lie.

``message`` is a PHI-free English one-liner. It is deliberately built
from the kind alone and never from an exception string: the detail column
is free text assembled from whatever ffmpeg or CUDA said, which can quote
the audio it choked on (ADR-0031 — the same reason the notification
fan-out ships ``error_kind`` and nothing else).

No DB CHECK constraint mirrors this enum on purpose. The worker deploys
independently of the migrations; a CHECK would turn "new worker, old
schema" into an unwritable failure path — the one path that must always
be writable. Unknown values decode to :data:`UNKNOWN_SPEC` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ErrorStage(StrEnum):
    """Where a job died. Also the natural grouping for dashboards."""

    SUBMIT = "submit"
    """Rejected by ``POST /asr/jobs``; no job row was ever created."""

    QUEUE = "queue"
    """Between enqueue and the worker claiming the message."""

    DECODE = "decode"
    """Fetching, decrypting, or decoding the audio into PCM."""

    INFERENCE = "inference"
    """Inside Whisper."""

    PERSIST = "persist"
    """Storing the transcript or recording the terminal status."""

    LIFECYCLE = "lifecycle"
    """The job outlived the process or the retry budget that owned it."""


class JobErrorKind(StrEnum):
    """Every value ``transcription_jobs.error_kind`` may hold."""

    # ── queue ────────────────────────────────────────────────────────
    ENQUEUE_FAILED = "enqueue_failed"
    QUEUE_LOST = "queue_lost"
    BAD_PAYLOAD = "bad_payload"
    JOB_ROW_MISSING = "job_row_missing"

    # ── decode ───────────────────────────────────────────────────────
    AUDIO_MISSING = "audio_missing"
    DECRYPT_FAILED = "decrypt_failed"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    CORRUPT_AUDIO = "corrupt_audio"
    NO_SPEECH = "no_speech"

    # ── inference ────────────────────────────────────────────────────
    MODEL_UNAVAILABLE = "model_unavailable"
    GPU_OOM = "gpu_oom"
    TIMEOUT = "timeout"

    # ── persist ──────────────────────────────────────────────────────
    RESULT_STORE_FAILED = "result_store_failed"
    DB_UNAVAILABLE = "db_unavailable"

    # ── lifecycle ────────────────────────────────────────────────────
    RETRY_EXHAUSTED = "retry_exhausted"
    WORKER_LOST = "worker_lost"

    # ── catch-all ────────────────────────────────────────────────────
    UNHANDLED = "unhandled"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """What one kind means, and what anyone reading it should do."""

    kind: str
    stage: ErrorStage
    retryable: bool
    resubmittable: bool
    message: str


def _spec(
    kind: JobErrorKind,
    stage: ErrorStage,
    *,
    retryable: bool,
    resubmittable: bool,
    message: str,
) -> ErrorSpec:
    return ErrorSpec(
        kind=str(kind),
        stage=stage,
        retryable=retryable,
        resubmittable=resubmittable,
        message=message,
    )


ERROR_SPECS: Final[dict[str, ErrorSpec]] = {
    str(s.kind): s
    for s in (
        _spec(
            JobErrorKind.ENQUEUE_FAILED,
            ErrorStage.QUEUE,
            retryable=False,
            resubmittable=True,
            message=(
                "The job was recorded but could not be handed to the "
                "transcription queue. Submit the recording again."
            ),
        ),
        _spec(
            JobErrorKind.QUEUE_LOST,
            ErrorStage.QUEUE,
            retryable=False,
            resubmittable=True,
            message=(
                "The job waited in the queue without ever being picked up by "
                "a worker and was timed out."
            ),
        ),
        _spec(
            JobErrorKind.BAD_PAYLOAD,
            ErrorStage.QUEUE,
            retryable=False,
            resubmittable=True,
            message=(
                "The queued job description could not be read by the worker "
                "(service/worker version skew)."
            ),
        ),
        _spec(
            JobErrorKind.JOB_ROW_MISSING,
            ErrorStage.QUEUE,
            retryable=False,
            resubmittable=True,
            message="The job record no longer exists; the queued work was dropped.",
        ),
        _spec(
            JobErrorKind.AUDIO_MISSING,
            ErrorStage.DECODE,
            retryable=False,
            resubmittable=True,
            message=(
                "The uploaded audio object is gone (retention, erasure, or an "
                "upload that never completed). Upload the recording again."
            ),
        ),
        _spec(
            JobErrorKind.DECRYPT_FAILED,
            ErrorStage.DECODE,
            retryable=False,
            resubmittable=False,
            message=(
                "The audio could not be decrypted — the envelope key is "
                "unavailable or the stored object does not match its metadata."
            ),
        ),
        _spec(
            JobErrorKind.STORAGE_UNAVAILABLE,
            ErrorStage.DECODE,
            retryable=True,
            resubmittable=False,
            message="Object storage was unreachable while fetching the audio.",
        ),
        _spec(
            JobErrorKind.CORRUPT_AUDIO,
            ErrorStage.DECODE,
            retryable=False,
            resubmittable=True,
            message=(
                "The audio passed upload validation but could not be decoded "
                "(truncated file, or a container whose declared codec the "
                "stream does not actually carry)."
            ),
        ),
        _spec(
            JobErrorKind.NO_SPEECH,
            ErrorStage.DECODE,
            retryable=False,
            resubmittable=True,
            message=(
                "The recording decoded cleanly but contains no speech — "
                "silence, or a microphone that captured nothing."
            ),
        ),
        _spec(
            JobErrorKind.MODEL_UNAVAILABLE,
            ErrorStage.INFERENCE,
            retryable=True,
            resubmittable=False,
            message="The transcription model is not loaded on the worker.",
        ),
        _spec(
            JobErrorKind.GPU_OOM,
            ErrorStage.INFERENCE,
            retryable=False,
            resubmittable=True,
            message=(
                "The GPU ran out of memory transcribing this recording. A "
                "shorter recording, or the same one once the fleet is less "
                "busy, will usually go through."
            ),
        ),
        _spec(
            JobErrorKind.TIMEOUT,
            ErrorStage.INFERENCE,
            retryable=False,
            resubmittable=True,
            message=(
                "Transcription exceeded the time budget allowed for a "
                "recording of this length."
            ),
        ),
        _spec(
            JobErrorKind.RESULT_STORE_FAILED,
            ErrorStage.PERSIST,
            retryable=True,
            resubmittable=False,
            message=(
                "The transcript was produced but could not be stored; it was "
                "not kept."
            ),
        ),
        _spec(
            JobErrorKind.DB_UNAVAILABLE,
            ErrorStage.PERSIST,
            retryable=True,
            resubmittable=False,
            message="The database was unreachable while recording the outcome.",
        ),
        _spec(
            JobErrorKind.RETRY_EXHAUSTED,
            ErrorStage.LIFECYCLE,
            retryable=False,
            resubmittable=True,
            message=(
                "The job failed on every delivery attempt and was moved to the "
                "dead-letter queue."
            ),
        ),
        _spec(
            JobErrorKind.WORKER_LOST,
            ErrorStage.LIFECYCLE,
            retryable=False,
            resubmittable=True,
            message=(
                "The worker transcribing this job stopped without finishing "
                "it, and the job was collected by the reaper."
            ),
        ),
        _spec(
            JobErrorKind.UNHANDLED,
            ErrorStage.INFERENCE,
            retryable=True,
            resubmittable=False,
            message="The job failed for an unclassified reason.",
        ),
    )
}

UNKNOWN_SPEC: Final = ErrorSpec(
    kind=str(JobErrorKind.UNHANDLED),
    stage=ErrorStage.INFERENCE,
    retryable=False,
    resubmittable=False,
    message="The job failed for a reason this build does not recognise.",
)
"""Decoded shape for a kind written by a newer worker than the reader.

Deliberately ``retryable=False``: a reader that cannot name the failure is
in no position to promise the failure is temporary.
"""


def spec_for(kind: str | None) -> ErrorSpec | None:
    """Look up ``kind``; ``None`` in, ``None`` out; unknown → :data:`UNKNOWN_SPEC`."""
    if not kind:
        return None
    return ERROR_SPECS.get(str(kind), UNKNOWN_SPEC)


def is_retryable(kind: str | None) -> bool:
    """Whether re-delivering the same message is worth the GPU time."""
    spec = spec_for(kind)
    return bool(spec and spec.retryable)


__all__ = [
    "ERROR_SPECS",
    "UNKNOWN_SPEC",
    "ErrorSpec",
    "ErrorStage",
    "JobErrorKind",
    "is_retryable",
    "spec_for",
]
