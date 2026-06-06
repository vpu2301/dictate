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

Failure modes:
  - ``AudioDecodeError``  → ``error_kind='corrupt_audio'``, no retry.
  - GPU OOM               → ``error_kind='gpu_oom'``, no retry; releases CUDA cache.
  - Inference timeout     → ``error_kind='timeout'``, no retry.
  - Other exceptions      → ``error_kind='unhandled'``; consumer.fail() retries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from uuid import UUID

from opentelemetry import metrics

from asr_models import JobEnqueuePayload, JobStatus, TranscriptionOutput
from audit import Severity
from db import tenant_connection
from messaging import Message

from . import audit_kinds
from .audio_io import AudioDecodeError, decode_to_pcm
from .config import settings
from .main_deps import WorkerState

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
            except Exception as exc:  # noqa: BLE001
                # Bubble up to consumer.fail() so retries + DLQ are handled.
                logger.exception("processor.unhandled", exc_info=exc)
                await consumer.fail(msg, error_kind="unhandled")


class _NonRetryableError(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


async def _process_one(state: WorkerState, msg: Message) -> None:
    payload = JobEnqueuePayload.model_validate_json(msg.value.decode("utf-8"))
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
            raise _NonRetryableError("job_row_missing", "job_id not in DB")
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
    try:
        ciphertext_key = f"{tenant_id}/{payload.audio_id}.enc"
        audio_bytes = await state.audio_store.get(
            key=ciphertext_key,
            tenant_id=tenant_id,
            aad=payload.audio_id.bytes,
        )

        try:
            pcm = await decode_to_pcm(
                audio_bytes,
                ffmpeg_path=settings.ffmpeg_path,
                timeout_seconds=settings.ffmpeg_timeout_seconds,
            )
        except AudioDecodeError as exc:
            await _mark_failed(
                state, tenant_id, job_id, kind="corrupt_audio", detail=str(exc)
            )
            raise _NonRetryableError("corrupt_audio", str(exc)) from exc

        audio_seconds = pcm.shape[0] / 16_000.0
        _audio_duration_seconds.record(audio_seconds)

        # Check cancel between fetch and inference.
        if await _is_cancelled(state, tenant_id, job_id):
            await _mark_cancelled(state, tenant_id, job_id)
            return

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
                ),
                timeout=max_infer,
            )
        except asyncio.TimeoutError:
            await _mark_failed(
                state, tenant_id, job_id, kind="timeout",
                detail=f"inference exceeded {max_infer:.1f}s",
            )
            raise _NonRetryableError("timeout", "inference timeout")
        except _CudaOOM as exc:
            _oom_counter.add(1)
            await _mark_failed(state, tenant_id, job_id, kind="gpu_oom", detail=str(exc))
            _release_cuda_cache()
            raise _NonRetryableError("gpu_oom", str(exc)) from exc

        infer_seconds = time.monotonic() - t0
        _inference_seconds.record(infer_seconds)
        if audio_seconds > 0:
            _realtime_factor.record(audio_seconds / max(infer_seconds, 1e-6))
        _gpu_memory_peak.record(output.metadata.peak_gpu_mem_mb)

        result_key = f"{tenant_id}/{job_id}.json.enc"
        body = output.model_dump_json().encode("utf-8")
        await state.transcript_store.put(
            key=result_key,
            plaintext=body,
            tenant_id=tenant_id,
            aad=job_id.bytes,
        )

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
    except _NonRetryableError:
        raise
    except Exception as exc:
        # Last-chance translation: anything we recognise as CUDA OOM
        # becomes a non-retryable error to avoid hammering the GPU.
        if _looks_like_oom(exc):
            _oom_counter.add(1)
            await _mark_failed(
                state, tenant_id, job_id, kind="gpu_oom", detail=str(exc)
            )
            _release_cuda_cache()
            raise _NonRetryableError("gpu_oom", str(exc)) from exc
        raise


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
) -> None:
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


async def _fetch_prompt(state: WorkerState, prompt_id: UUID) -> str | None:
    async with state.app_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT prompt_text FROM medical_prompts WHERE id = $1",
            prompt_id,
        )
    return str(row["prompt_text"]) if row is not None else None


class _CudaOOM(RuntimeError):
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
    return datetime.now(timezone.utc).isoformat()
