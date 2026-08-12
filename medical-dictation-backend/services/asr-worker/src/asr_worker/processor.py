"""Main job processor — Whisper inference loop.

Lifecycle of a job:

1. Pull message from Redis Streams via ``RedisStreamsConsumer``.
2. Parse :class:`JobEnqueuePayload` from the message value.
3. Idempotency check: SELECT status from transcription_jobs.
4. Mark running, audit ``asr.transcription_started``.
5. Fetch encrypted audio bytes from MinIO via ``EncryptedObjectStore``.
6. Decode via ffmpeg into mono 16 kHz float32 PCM.
7. Fetch the medical_prompts row for ``prompt_id``.
8. Run ``WhisperEngine.transcribe``.
9. Serialize :class:`TranscriptionOutput` JSON; encrypt + upload.
10. Mark complete, audit ``asr.transcription_complete``.
11. ACK the Redis message.

Failure modes are the closed vocabulary in :mod:`asr_models.errors`, and
the vocabulary decides the retry: a kind whose spec says ``retryable`` goes
back to ``consumer.fail()`` for redelivery, everything else is recorded on
the row and acked. Re-delivering a corrupt file three more times only
delays the failure the clinician is already waiting on.

  - ``AudioDecodeError``      → ``corrupt_audio``        (terminal)
  - decoded PCM has no speech → ``no_speech``            (terminal)
  - audio object gone         → ``audio_missing``        (terminal)
  - envelope/AAD failure      → ``decrypt_failed``       (terminal)
  - object store unreachable  → ``storage_unavailable``  (retried)
  - model not loaded          → ``model_unavailable``    (retried)
  - CUDA OOM                  → ``gpu_oom``              (terminal; frees cache)
  - inference over budget     → ``timeout``              (terminal)
  - transcript upload failed  → ``result_store_failed``  (retried)
  - anything else             → ``unhandled``            (retried → DLQ)

Whatever the kind, the row reaches a terminal status before the message is
acked. A failure the worker knows about and the ``transcription_jobs`` row
does not is the one outcome this module must never produce: the job would
sit in ``running`` forever, holding a slot in the tenant's concurrency
budget and showing a spinner nobody will ever resolve. The retry-exhausted
path (DLQ) and the reaper in asr-service exist for the two cases where the
worker cannot write that row itself.

Cancellation is checked at four points, because ``DELETE /asr/jobs/{id}``
on a RUNNING job can only ask: it sets ``cancel_requested`` and leaves the
status alone, so nothing stops unless the worker looks. It looks before
claiming the job, after decoding the audio, between inference chunks, and
once more before the transcript is stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from opentelemetry import metrics

from asr_models import JobEnqueuePayload, JobErrorKind, TranscriptionOutput, spec_for
from audit import Severity
from crypto import CryptoError
from db import tenant_connection
from messaging import Message, RedisStreamsConsumer
from storage import ObjectNotFoundError

from . import audit_kinds
from .audio_io import AudioDecodeError, decode_to_pcm
from .config import settings
from .inference import TranscriptionCancelledError
from .main_deps import WorkerState
from .notifications import emit_transcription_completed, emit_transcription_failed

logger = logging.getLogger(__name__)

_meter = metrics.get_meter("mdx.asr.worker")
_inference_seconds = _meter.create_histogram(
    "mdx_asr_inference_seconds",
    description="Inference wall-clock per job",
    unit="s",
)
_audio_duration_seconds = _meter.create_histogram(
    "mdx_asr_audio_duration_seconds",
    description="Audio duration per job",
    unit="s",
)
_realtime_factor = _meter.create_histogram(
    "mdx_asr_realtime_factor",
    description="audio_duration / infer_seconds (>1 = faster than realtime)",
    unit="1",
)
_gpu_memory_peak = _meter.create_histogram(
    "mdx_asr_gpu_memory_peak_mb",
    description="Peak GPU memory per job",
    unit="MB",
)
_oom_counter = _meter.create_counter(
    "mdx_asr_oom_total",
    description="Times the worker hit CUDA OOM",
    unit="1",
)
_warmup_gauge = _meter.create_gauge(
    "mdx_asr_warmup_seconds",
    description="Worker warmup duration on startup",
    unit="s",
)
_model_loaded_gauge = _meter.create_gauge(
    "mdx_asr_model_loaded",
    description="1 if Whisper model is loaded",
    unit="1",
)


async def run_forever(state: WorkerState) -> None:
    """Top-level loop. Consumes the queue until SIGTERM."""
    _warmup_gauge.set(state.engine.warmup_seconds)
    _model_loaded_gauge.set(1 if state.engine.is_loaded else 0)

    async with state.consumer as consumer:
        async for msg in consumer:
            try:
                await _process_one(state, msg)
                await consumer.ack(msg)
            except _NonRetryableError as exc:
                # Already recorded a failure on the job row; ack so the
                # message doesn't keep getting redelivered.
                logger.info(
                    "processor.non_retryable",
                    extra={"reason": exc.kind, "detail": str(exc)},
                )
                await consumer.ack(msg)
            except _RetryableError as exc:
                # Transient by classification — hand it back for redelivery.
                # The job row is left in `running` on purpose: it IS still
                # running, on the next attempt. Only exhaustion is terminal.
                logger.warning(
                    "processor.retryable",
                    extra={"reason": exc.kind, "detail": str(exc)},
                )
                await _fail_or_retry(state, consumer, msg, exc)
            except Exception as exc:  # noqa: BLE001
                logger.exception("processor.unhandled", exc_info=exc)
                await _fail_or_retry(
                    state,
                    consumer,
                    msg,
                    _RetryableError(str(JobErrorKind.UNHANDLED), str(exc)),
                )


async def _fail_or_retry(
    state: WorkerState,
    consumer: RedisStreamsConsumer,
    msg: Message,
    exc: _JobError,
) -> None:
    """Hand a retryable failure back to the queue; land it if that was the last try.

    ``consumer.fail`` reports whether this attempt exhausted the retry
    budget and moved the message to the DLQ. That was previously the end of
    the story from the queue's side and the beginning of a silence on the
    database's: the message left the stream, and the job row stayed in
    ``running`` with no worker, no retry, and no explanation — indefinitely.
    A DLQ'd message is a dead job, and the row has to say so.
    """
    dead_lettered = await consumer.fail(msg, error_kind=exc.kind)
    if not dead_lettered:
        return
    ids = _identify(msg)
    if ids is None:
        # Unparseable payload — there is no row to fail. The DLQ entry is
        # the whole record, which is why bad_payload is never retried.
        return
    tenant_id, job_id, requester_sub = ids
    logger.error(
        "processor.retry_exhausted",
        extra={"job_id": str(job_id), "last_error_kind": exc.kind},
    )
    # Suppressed: if the database is what's failing, this write fails too.
    # The reaper in asr-service is the backstop for exactly that case.
    with contextlib.suppress(Exception):
        await _mark_failed(
            state,
            tenant_id,
            job_id,
            kind=str(JobErrorKind.RETRY_EXHAUSTED),
            detail=f"dead-lettered after repeated {exc.kind}: {exc.detail}",
            requester_sub=requester_sub,
        )


def _identify(msg: Message) -> tuple[UUID, UUID, UUID] | None:
    """(tenant_id, job_id, requester_sub) from a queue message, if it parses."""
    try:
        payload = JobEnqueuePayload.model_validate_json(msg.value.decode("utf-8"))
    except Exception:  # noqa: BLE001 — any parse failure means "no job to name"
        return None
    return payload.tenant_id, payload.job_id, payload.requester_sub


# How often the engine may ask the database whether the clinician has
# cancelled. Between VAD chunks, so the real granularity is whichever is
# coarser — one chunk, or one second.
_CANCEL_POLL_SECONDS = 1.0


class _JobError(Exception):
    """A classified failure. ``kind`` is always a :class:`JobErrorKind` value."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class _NonRetryableError(_JobError):
    """Terminal: the row already says failed, and redelivery cannot help."""


class _RetryableError(_JobError):
    """Transient: hand the message back; the same job may yet succeed."""


def _classified(kind: JobErrorKind, detail: str) -> _JobError:
    """Build the right exception for ``kind`` straight from its spec.

    Retry policy lives in the vocabulary, not at the raise site — so
    ``storage_unavailable`` cannot be spelled retryable in one branch and
    terminal in the next.
    """
    spec = spec_for(str(kind))
    cls = _RetryableError if spec is not None and spec.retryable else _NonRetryableError
    return cls(str(kind), detail)


async def _process_one(state: WorkerState, msg: Message) -> None:
    try:
        payload = JobEnqueuePayload.model_validate_json(msg.value.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — decode or schema, same dead end
        # Version skew: asr-service enqueued a shape this worker cannot
        # read. Retrying re-reads the same bytes to the same conclusion, so
        # this goes straight to the DLQ where an operator can see it.
        logger.error("processor.bad_payload", extra={"error": str(exc)})
        raise _NonRetryableError(
            str(JobErrorKind.BAD_PAYLOAD), f"{type(exc).__name__}: {exc}"
        ) from exc
    tenant_id = payload.tenant_id
    job_id = payload.job_id

    # Idempotency: check the row before doing work.
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT status, cancel_requested FROM transcription_jobs WHERE id = $1",
            job_id,
        )
        if row is None:
            logger.warning(
                "processor.job_row_missing",
                extra={"job_id": str(job_id), "tenant_id": str(tenant_id)},
            )
            raise _NonRetryableError(
                str(JobErrorKind.JOB_ROW_MISSING), "job_id not in DB"
            )
        if row["status"] in {"complete", "failed"}:
            logger.info(
                "processor.idempotent_skip",
                extra={"job_id": str(job_id), "status": row["status"]},
            )
            return
        if row["status"] == "cancelled":
            return
        if row["cancel_requested"]:
            await _mark_cancelled(state, tenant_id, job_id)
            return
        # Move the row to running.
        await conn.execute(
            """
            UPDATE transcription_jobs
            SET status='running', started_at=now(), attempts=attempts+1
            WHERE id = $1 AND status IN ('queued','running')
            """,
            job_id,
        )

    await state.audit_writer.write_event(
        tenant_id=tenant_id,
        kind=audit_kinds.TRANSCRIPTION_STARTED,
        target_kind="asr_job",
        target_id=str(job_id),
        payload={"audio_id": str(payload.audio_id)},
        severity=Severity.INFO,
    )

    t0 = time.monotonic()
    # Every classified failure below leaves through `die`, which records the
    # terminal ones on the row before raising. Retryable kinds deliberately
    # leave the row in `running`: the job has not failed, this attempt has.
    die = _dier(state, tenant_id, job_id, requester_sub=payload.requester_sub)
    try:
        ciphertext_key = f"{tenant_id}/{payload.audio_id}.enc"
        try:
            audio_bytes = await state.audio_store.get(
                key=ciphertext_key,
                tenant_id=tenant_id,
                aad=payload.audio_id.bytes,
            )
        except ObjectNotFoundError as exc:
            # Retention, the S11 erasure engine, or an upload whose row was
            # written but whose object never landed. The bytes are not
            # coming back — no amount of redelivery finds them.
            raise await die(JobErrorKind.AUDIO_MISSING, str(exc)) from exc
        except CryptoError as exc:
            # Wrong AAD, a tenant KEK that will not unwrap, a truncated
            # envelope. Deterministic, and an operator's problem — the
            # clinician re-uploading the same file changes nothing.
            raise await die(JobErrorKind.DECRYPT_FAILED, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — S3/MinIO transport
            raise await die(JobErrorKind.STORAGE_UNAVAILABLE, str(exc)) from exc

        try:
            pcm = await decode_to_pcm(
                audio_bytes,
                ffmpeg_path=settings.ffmpeg_path,
                timeout_seconds=settings.ffmpeg_timeout_seconds,
            )
        except AudioDecodeError as exc:
            raise await die(JobErrorKind.CORRUPT_AUDIO, str(exc)) from exc

        audio_seconds = pcm.shape[0] / 16_000.0
        _audio_duration_seconds.record(audio_seconds)

        # ffmpeg is happy to decode a file into nothing — a container whose
        # audio stream is empty, or a recording that is pure silence. Whisper
        # given no samples answers with a hallucinated phrase, and a
        # hallucination stored as a `complete` transcript is worse than any
        # failure: it reaches the chart looking like something the clinician
        # said.
        if audio_seconds <= 0:
            raise await die(
                JobErrorKind.NO_SPEECH, "decoded audio contains no samples"
            )

        # Check cancel between fetch and inference.
        if await _is_cancelled(state, tenant_id, job_id):
            await _mark_cancelled(state, tenant_id, job_id)
            return

        if not state.engine.is_loaded:
            # The readiness probe should have caught this; if it did not,
            # say so rather than letting the engine's bare RuntimeError get
            # filed as `unhandled`.
            raise await die(
                JobErrorKind.MODEL_UNAVAILABLE, "whisper model is not loaded"
            )

        prompt_text = await _fetch_prompt(state, payload.prompt_id)

        max_infer = max(
            60.0,
            audio_seconds * settings.asr_max_inference_seconds_multiplier,
        )
        try:
            output: TranscriptionOutput = await asyncio.wait_for(
                state.engine.transcribe(
                    pcm,
                    language=payload.language,
                    prompt=prompt_text,
                    prompt_id=payload.prompt_id,
                    # Cancel is a request, not a status: DELETE /asr/jobs/{id}
                    # on a RUNNING job only sets `cancel_requested`, and it is
                    # the worker that has to act on it. Before this it never
                    # looked again after inference started, so pressing Cancel
                    # on a job that was already transcribing did nothing at
                    # all — the job ran to completion and came back `complete`.
                    should_cancel=_cancel_poller(state, tenant_id, job_id),
                ),
                timeout=max_infer,
            )
        except TranscriptionCancelledError:
            await _mark_cancelled(state, tenant_id, job_id)
            return
        except TimeoutError:
            raise await die(
                JobErrorKind.TIMEOUT, f"inference exceeded {max_infer:.1f}s"
            ) from None
        except _CudaOOMError as exc:
            _oom_counter.add(1)
            err = await die(JobErrorKind.GPU_OOM, str(exc))
            _release_cuda_cache()
            raise err from exc

        # Inference ran and produced nothing. Same reasoning as the empty-PCM
        # gate above, one stage later: a zero-segment transcript stored as
        # `complete` reads to the clinician as "we transcribed your recording
        # and it was blank", which is indistinguishable from a lost dictation.
        if not output.segments:
            raise await die(
                JobErrorKind.NO_SPEECH,
                f"no speech recognised in {audio_seconds:.1f}s of audio",
            )

        infer_seconds = time.monotonic() - t0
        _inference_seconds.record(infer_seconds)
        if audio_seconds > 0:
            _realtime_factor.record(audio_seconds / max(infer_seconds, 1e-6))
        _gpu_memory_peak.record(output.metadata.peak_gpu_mem_mb)

        # Last look before the transcript becomes a fact. A cancel that
        # landed during the final chunk, or while the audio was being
        # decoded, must not be overwritten by a `complete` — the clinician
        # asked for this job to stop, and a stored transcript is not stopping.
        if await _is_cancelled(state, tenant_id, job_id):
            await _mark_cancelled(state, tenant_id, job_id)
            return

        result_key = f"{tenant_id}/{job_id}.json.enc"
        body = output.model_dump_json().encode("utf-8")
        try:
            await state.transcript_store.put(
                key=result_key,
                plaintext=body,
                tenant_id=tenant_id,
                aad=job_id.bytes,
            )
        except Exception as exc:  # noqa: BLE001 — encrypt or transport
            # Retryable, and the retry redoes the inference. That is the
            # cheaper mistake: the alternative is a job marked complete
            # pointing at an object that was never written, which fails much
            # later, on read, as a 410 the clinician cannot act on.
            raise await die(JobErrorKind.RESULT_STORE_FAILED, str(exc)) from exc

        try:
            async with tenant_connection(state.app_pool, tenant_id) as conn:
                await conn.execute(
                    """
                    UPDATE transcription_jobs
                    SET status='complete',
                        result_storage_uri=$2,
                        finished_at=now(),
                        metadata=$3::jsonb
                    WHERE id = $1
                    """,
                    job_id,
                    f"minio://{state.transcript_store.bucket}/{result_key}",
                    json.dumps(output.metadata.model_dump(mode="json")),
                )
                await conn.execute(
                    "UPDATE audio_files SET status='transcribed' WHERE id = $1",
                    payload.audio_id,
                )
        except Exception as exc:  # noqa: BLE001 — asyncpg transport / pool
            # The transcript is stored; only the bookkeeping failed. The
            # redelivery re-runs inference and overwrites the same key, so
            # this is safe to retry — and unlike the alternative it does not
            # strand a finished transcript behind a `running` row.
            raise await die(JobErrorKind.DB_UNAVAILABLE, str(exc)) from exc

        await state.audit_writer.write_event(
            tenant_id=tenant_id,
            kind=audit_kinds.TRANSCRIPTION_COMPLETE,
            target_kind="asr_job",
            target_id=str(job_id),
            payload={
                "audio_seconds": round(audio_seconds, 2),
                "infer_seconds": round(infer_seconds, 2),
                "realtime_factor": round(audio_seconds / max(infer_seconds, 1e-6), 2),
                "peak_gpu_mem_mb": output.metadata.peak_gpu_mem_mb,
                "model": output.metadata.model,
                "segments": len(output.segments),
            },
            severity=Severity.INFO,
        )

        # After the status UPDATE and the audit write, outside the
        # tenant_connection block — the same shape report-service uses.
        # A job the user submitted and stopped watching is the strongest
        # case in the system for a notification.
        await emit_transcription_completed(
            state.redis,
            tenant_id=tenant_id,
            job_id=job_id,
            requester_sub=payload.requester_sub,
            duration_ms=int(audio_seconds * 1000),
            segments=len(output.segments),
            language=payload.language,
            model=output.metadata.model,
        )
    except _JobError:
        raise
    except Exception as exc:
        # Last-chance translation: anything we recognise as CUDA OOM
        # becomes a non-retryable error to avoid hammering the GPU.
        if _looks_like_oom(exc):
            _oom_counter.add(1)
            err = await die(JobErrorKind.GPU_OOM, str(exc))
            _release_cuda_cache()
            raise err from exc
        raise


def _dier(
    state: WorkerState, tenant_id: UUID, job_id: UUID, *, requester_sub: UUID
) -> Callable[[JobErrorKind, str], Awaitable[_JobError]]:
    """Build the ``die(kind, detail)`` used by every classified failure.

    Returns (rather than raises) the exception so call sites read
    ``raise await die(...) from exc`` and keep the original traceback
    chained — the detail column is the only place the underlying ffmpeg or
    CUDA text survives, and losing the ``__cause__`` would cost the log its
    stack.

    Terminal kinds are written to the row here, once, before the raise;
    retryable kinds are not, because the job has not finished failing.
    """

    async def die(kind: JobErrorKind, detail: str) -> _JobError:
        err = _classified(kind, detail)
        if isinstance(err, _NonRetryableError):
            await _mark_failed(
                state,
                tenant_id,
                job_id,
                kind=str(kind),
                detail=detail,
                requester_sub=requester_sub,
            )
        return err

    return die


def _cancel_poller(
    state: WorkerState, tenant_id: UUID, job_id: UUID
) -> Callable[[], Awaitable[bool]]:
    """`should_cancel` for the engine, rate-limited to one query a second.

    VAD can cut a long consultation into hundreds of speech runs, and a
    round trip per run would spend more time asking whether to stop than
    stopping saves. A second of extra inference is not worth a query storm.
    """
    last = 0.0
    answer = False

    async def poll() -> bool:
        nonlocal last, answer
        now = time.monotonic()
        if answer or now - last < _CANCEL_POLL_SECONDS:
            return answer
        last = now
        answer = await _is_cancelled(state, tenant_id, job_id)
        return answer

    return poll


async def _is_cancelled(state: WorkerState, tenant_id: UUID, job_id: UUID) -> bool:
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT cancel_requested FROM transcription_jobs WHERE id = $1",
            job_id,
        )
    return bool(row and row["cancel_requested"])


async def _mark_cancelled(state: WorkerState, tenant_id: UUID, job_id: UUID) -> None:
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        await conn.execute(
            """
            UPDATE transcription_jobs
            SET status='cancelled', finished_at=now()
            WHERE id = $1 AND status NOT IN ('complete','failed','cancelled')
            """,
            job_id,
        )
    await state.audit_writer.write_event(
        tenant_id=tenant_id,
        kind=audit_kinds.JOB_CANCELLED,
        target_kind="asr_job",
        target_id=str(job_id),
        payload={"actor": "worker"},
        severity=Severity.INFO,
    )


async def _mark_failed(
    state: WorkerState,
    tenant_id: UUID,
    job_id: UUID,
    *,
    kind: str,
    detail: str,
    requester_sub: UUID,
) -> None:
    """Terminal-failure path. Every failure funnels through here.

    `requester_sub` is threaded in rather than re-SELECTed: the row is
    about to be UPDATEd anyway, and a second query for a value the caller
    already holds in `payload` is a round trip for nothing.
    """
    async with tenant_connection(state.app_pool, tenant_id) as conn:
        await conn.execute(
            """
            UPDATE transcription_jobs
            SET status='failed', error_kind=$2, error_detail=$3, finished_at=now()
            WHERE id = $1
            """,
            job_id,
            kind,
            detail[:1024],
        )
    await state.audit_writer.write_event(
        tenant_id=tenant_id,
        kind=audit_kinds.TRANSCRIPTION_FAILED,
        target_kind="asr_job",
        target_id=str(job_id),
        payload={"error_kind": kind, "detail": detail[:200]},
        severity=Severity.ERROR,
    )

    # `kind` only, never `detail` — see emit_transcription_failed.
    await emit_transcription_failed(
        state.redis,
        tenant_id=tenant_id,
        job_id=job_id,
        requester_sub=requester_sub,
        error_kind=kind,
    )


async def _fetch_prompt(state: WorkerState, prompt_id: UUID) -> str | None:
    async with state.app_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT prompt_text FROM medical_prompts WHERE id = $1",
            prompt_id,
        )
    return str(row["prompt_text"]) if row is not None else None


class _CudaOOMError(RuntimeError):
    pass


def _looks_like_oom(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if "cuda out of memory" in msg or "outofmemory" in msg:
        return True
    try:
        import torch

        return isinstance(exc, torch.cuda.OutOfMemoryError)  # type: ignore[attr-defined]
    except Exception:
        return False


def _release_cuda_cache() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


# Re-export the timestamp helper for the worker tests.
def now_iso() -> str:
    return datetime.now(UTC).isoformat()
