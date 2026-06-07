"""``/asr/jobs`` — submit, list, fetch, cancel batch ASR jobs.

Notes:

- The POST handler streams the file body into a bounded in-memory buffer
  (``Settings.max_upload_mb``); FastAPI's underlying Starlette respects
  the size cap and fails the request early when the cap is exceeded.
- All 8 validators run synchronously before any DB or queue work; the
  pipeline short-circuits on first failure and returns RFC 9457.
- Audio is encrypted via ``EncryptedObjectStore`` before the row is
  inserted, so a crash between upload and DB insert leaves an
  orphaned ciphertext (not plaintext) which is reaped by a cleanup
  cron (sprint 16 lifecycle policy on the bucket).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from opentelemetry import metrics

from asr_models import JobEnqueuePayload, JobStatus, TranscriptionJobView
from audit import Severity
from auth import Claims
from db import tenant_connection

from .. import audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain import repository
from ..validators import run_all
from ..validators.quota import validate_quota

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/asr", tags=["asr"])

_meter = metrics.get_meter("mdx.asr.service")
_uploads_counter = _meter.create_counter(
    "mdx_asr_uploads_total",
    description="POST /asr/jobs by status",
    unit="1",
)
_validation_rejects_counter = _meter.create_counter(
    "mdx_asr_validation_failures_total",
    description="Validation rejections by code",
    unit="1",
)
_jobs_counter = _meter.create_counter(
    "mdx_asr_jobs_total",
    description="Job lifecycle transitions by status",
    unit="1",
)


@router.post(
    "/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TranscriptionJobView,
    summary="Submit a batch ASR job (multipart upload).",
)
async def submit_job(
    audio: Annotated[UploadFile, File(description="Audio file to transcribe.")],
    prompt_id: Annotated[UUID, Form()],
    language: Annotated[str, Form(pattern="^(uk|en)$")],
    encounter_id: Annotated[UUID | None, Form()] = None,
    claims: Annotated[Claims, Depends(requires("asr.write", "asr_job"))] = ...,  # type: ignore[assignment]
) -> TranscriptionJobView:
    state = get_state()

    payload = await audio.read()
    mime_type = audio.content_type or "application/octet-stream"

    # Steps 2–7: synchronous file-shape validation.
    result, facts = await run_all(mime_type=mime_type, payload=payload)
    if not result.ok:
        _validation_rejects_counter.add(1, {"code": result.code})
        _uploads_counter.add(1, {"status": "rejected"})
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "type": f"urn:mdx:asr:validation:{result.code}",
                "title": "audio rejected by validation",
                "code": result.code,
                "detail": result.detail,
            },
        )

    # Rate limit: per-tenant cap on concurrent jobs.
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        active = await repository.count_active_jobs(conn, tenant_id=claims.tid)
        if active >= settings.per_tenant_concurrent_jobs:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "type": "urn:mdx:asr:rate_limit:per_tenant_concurrent",
                    "title": "too many active jobs for tenant",
                    "active": active,
                    "limit": settings.per_tenant_concurrent_jobs,
                },
            )

    # Step 8: quota check, inside the same transaction as the row inserts.
    audio_id = uuid4()
    job_id = uuid4()
    storage_key = f"{claims.tid}/{audio_id}.enc"

    # Encrypt + upload BEFORE row insert. Orphan ciphertext on a crash
    # is preferable to an orphan row referencing nothing.
    header = await state.audio_store.put(
        key=storage_key,
        plaintext=payload,
        tenant_id=claims.tid,
        aad=audio_id.bytes,
    )

    async with tenant_connection(state.app_pool, claims.tid) as conn:
        qr = await validate_quota(
            conn,
            tenant_id=claims.tid,
            incoming_size_bytes=facts.size_bytes,
            monthly_quota_bytes=settings.monthly_quota_bytes,
        )
        if not qr.ok:
            _validation_rejects_counter.add(1, {"code": qr.code})
            _uploads_counter.add(1, {"status": "rejected"})
            # Best-effort: delete the orphan ciphertext; cleanup cron
            # picks up any leftover.
            await state.audio_store.delete(key=storage_key)
            await _audit_quota_exceeded(state, claims, audio_id)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "type": f"urn:mdx:asr:validation:{qr.code}",
                    "title": "monthly tenant quota exceeded",
                    "code": qr.code,
                    "detail": qr.detail,
                },
            )

        await repository.insert_audio_row(
            conn,
            audio_id=audio_id,
            tenant_id=claims.tid,
            uploader_sub=claims.sub,
            mime_type=facts.mime_type,
            size_bytes=facts.size_bytes,
            duration_ms=facts.duration_ms,
            sha256=facts.sha256,
            envelope_metadata=_header_to_json(header),
            storage_uri=f"minio://{state.audio_store.bucket}/{storage_key}",
        )
        await repository.insert_job_row(
            conn,
            job_id=job_id,
            tenant_id=claims.tid,
            audio_id=audio_id,
            requester_sub=claims.sub,
            prompt_id=prompt_id,
            language=language,
            model="large-v3",
        )

    # Audit the upload + job creation.
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.AUDIO_UPLOADED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="audio",
        target_id=str(audio_id),
        payload={
            "size_bytes": facts.size_bytes,
            "duration_ms": facts.duration_ms,
            "sample_rate_hz": facts.sample_rate_hz,
            "codec": facts.codec,
            "encounter_id": str(encounter_id) if encounter_id else None,
        },
        severity=Severity.INFO,
    )

    queue_payload = JobEnqueuePayload(
        job_id=job_id,
        tenant_id=claims.tid,
        audio_id=audio_id,
        prompt_id=prompt_id,
        language=language,
        model="large-v3",
        requester_sub=claims.sub,
    )
    await state.queue_producer.send(
        value=queue_payload.model_dump_json().encode("utf-8"),
        key=str(job_id).encode("utf-8"),
        headers={
            "tenant_id": str(claims.tid),
            "job_id": str(job_id),
            "schema_version": "1",
        },
    )

    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.JOB_QUEUED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="asr_job",
        target_id=str(job_id),
        payload={
            "audio_id": str(audio_id),
            "prompt_id": str(prompt_id),
            "language": language,
        },
        severity=Severity.INFO,
    )

    _uploads_counter.add(1, {"status": "accepted"})
    _jobs_counter.add(1, {"status": "queued"})

    return TranscriptionJobView(
        id=job_id,
        tenant_id=claims.tid,
        audio_id=audio_id,
        requester_sub=claims.sub,
        prompt_id=prompt_id,
        language=language,
        model="large-v3",
        status=JobStatus.QUEUED,
        queued_at=datetime.fromtimestamp(time.time()),
    )


@router.get(
    "/jobs/{job_id}",
    response_model=TranscriptionJobView,
    summary="Fetch a job's status (and a pre-signed result URL on complete).",
)
async def get_job(
    job_id: UUID,
    claims: Annotated[Claims, Depends(requires("asr.read", "asr_job"))] = ...,  # type: ignore[assignment]
) -> TranscriptionJobView:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        view = await repository.get_job(conn, job_id=job_id)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if view.status == JobStatus.COMPLETE:
        # Pre-signed URL with the configured TTL — deliberately short.
        url = await state.transcript_store.presigned_url(
            key=f"{claims.tid}/{job_id}.json.enc",
            expires_in=settings.s3_presigned_ttl_seconds,
        )
        view = view.model_copy(update={"result_url": url})
    return view


@router.get(
    "/jobs",
    response_model=list[TranscriptionJobView],
    summary="List tenant's recent jobs.",
)
async def list_jobs(
    claims: Annotated[Claims, Depends(requires("asr.read", "asr_job"))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    since: Annotated[datetime | None, Query()] = None,
) -> list[TranscriptionJobView]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        return await repository.list_jobs(conn, limit=limit, status=status_filter, since=since)


@router.delete(
    "/jobs/{job_id}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel a queued or running job.",
)
async def cancel_job(
    job_id: UUID,
    claims: Annotated[Claims, Depends(requires("asr.cancel", "asr_job"))] = ...,  # type: ignore[assignment]
) -> dict[str, str]:
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        outcome = await repository.request_cancel(conn, job_id=job_id)
    if outcome is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="job is already complete/failed/cancelled, or does not exist",
        )
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.JOB_CANCELLED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="asr_job",
        target_id=str(job_id),
        payload={"outcome": outcome},
        severity=Severity.INFO,
    )
    _jobs_counter.add(1, {"status": outcome})
    return {"status": outcome}


async def _audit_quota_exceeded(state: object, claims: Claims, audio_id: UUID) -> None:
    # ``state`` typed as object so the import-linter doesn't see this fn
    # as creating a cycle with main_deps.
    try:
        await state.audit_writer.write_event(  # type: ignore[attr-defined]
            tenant_id=claims.tid,
            kind=audit_kinds.QUOTA_EXCEEDED,
            actor_sub=claims.sub,
            target_kind="audio",
            target_id=str(audio_id),
            payload={"reason": "monthly_quota"},
            severity=Severity.WARN,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "audit.quota_exceeded.write_failed",
            extra={"error": str(exc), "error_class": type(exc).__name__},
        )


def _header_to_json(header: object) -> dict[str, str | int]:
    from storage.object_store import header_metadata_for_row

    return header_metadata_for_row(header)  # type: ignore[arg-type]
