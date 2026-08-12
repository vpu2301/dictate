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
    Request,
    UploadFile,
    status,
)
from opentelemetry import metrics

from asr_models import (
    ConfidenceSpanView,
    EnrichedSegment,
    JobEnqueuePayload,
    JobErrorKind,
    JobStatus,
    TranscriptionJobView,
    TranscriptionOutput,
    TranscriptResultView,
)
from audit import Severity
from auth import Claims, can_claims
from db import tenant_connection
from storage import ObjectNotFoundError

from .. import audit_kinds
from ..config import settings
from ..deps import get_state, requires, requires_any
from ..domain import repository
from ..validators import ValidationCode, run_all
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


def _reject(
    code: ValidationCode | str,
    detail: str,
    *,
    title: str,
    status_code: int = status.HTTP_400_BAD_REQUEST,
    type_uri: str | None = None,
    **extra: object,
) -> HTTPException:
    """Build one submit-time rejection.

    Every reject on this endpoint — file shape, linkage, budget — leaves
    through here, so ``type``/``code``/``detail`` are assembled once and a
    client can switch on ``code`` alone. Counting happens here too: a
    rejection that is raised but never counted is a rejection nobody sees
    on the dashboard.

    ``problem_extras`` rather than a dict ``detail``: the shared handler
    renders ``str(exc.detail)``, so a dict arrives at the client as a
    stringified Python repr — single quotes and all — with the real
    document left at ``type: about:blank``. Extension members put ``code``
    and ``type`` where RFC 9457 says they go, and where a client can parse
    them. ``title`` is set by the handler from the status code and cannot
    be passed here, so it lands as ``reason``.
    """
    _validation_rejects_counter.add(1, {"code": str(code)})
    _uploads_counter.add(1, {"status": "rejected"})
    exc = HTTPException(status_code=status_code, detail=detail)
    exc.problem_extras = {  # type: ignore[attr-defined]
        "type_uri": type_uri or f"urn:mdx:asr:validation:{code}",
        "code": str(code),
        "reason": title,
        **extra,
    }
    return exc


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
        raise _reject(
            result.code, result.detail, title="audio rejected by validation"
        )

    # S11 step 02: a named encounter must exist in-tenant (RLS makes a
    # foreign one look nonexistent) and not be cancelled — rejected before
    # the ciphertext is even uploaded. Prompt check and rate limit share
    # the connection.
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        if encounter_id is not None:
            enc_status = await repository.fetch_encounter_status(
                conn, encounter_id=encounter_id
            )
            if enc_status is None:
                raise _reject(
                    ValidationCode.ENCOUNTER_INVALID,
                    "encounter not found in this tenant",
                    title="encounter linkage rejected",
                )
            if enc_status == "cancelled":
                raise _reject(
                    ValidationCode.ENCOUNTER_CLOSED,
                    "encounter is cancelled; upload is not allowed",
                    title="encounter linkage rejected",
                )
        # The FK on transcription_jobs.prompt_id would otherwise turn a
        # stale prompt id into a 500 at INSERT time, after the audio had
        # already been encrypted and uploaded.
        if not await repository.prompt_exists(conn, prompt_id=prompt_id):
            raise _reject(
                ValidationCode.PROMPT_INVALID,
                f"prompt {prompt_id} is not in the prompt catalogue",
                title="prompt rejected",
            )
        active = await repository.count_active_jobs(conn, tenant_id=claims.tid)
        if active >= settings.per_tenant_concurrent_jobs:
            raise _reject(
                ValidationCode.CONCURRENCY_EXCEEDED,
                (
                    f"tenant has {active} queued/running jobs; the concurrent "
                    f"limit is {settings.per_tenant_concurrent_jobs}. Wait for "
                    "one to finish and resubmit."
                ),
                title="too many active jobs for tenant",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                # Pre-dates the `code` field and clients may match on it.
                type_uri="urn:mdx:asr:rate_limit:per_tenant_concurrent",
                active=active,
                limit=settings.per_tenant_concurrent_jobs,
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
            # Best-effort: delete the orphan ciphertext; cleanup cron
            # picks up any leftover.
            await state.audio_store.delete(key=storage_key)
            await _audit_quota_exceeded(state, claims, audio_id)
            raise _reject(
                qr.code,
                qr.detail,
                title="monthly tenant quota exceeded",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
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
            encounter_id=encounter_id,
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
    try:
        await state.queue_producer.send(
            value=queue_payload.model_dump_json().encode("utf-8"),
            key=str(job_id).encode("utf-8"),
            headers={
                "tenant_id": str(claims.tid),
                "job_id": str(job_id),
                "schema_version": "1",
            },
        )
    except Exception as exc:  # noqa: BLE001 — every publish failure is the same failure
        # The row exists and the audio is stored, but nothing will ever
        # transcribe it. Left as-is the job sits in `queued` forever, holds
        # a slot in the tenant's concurrency budget, and shows the
        # clinician a spinner for work that was never handed to anyone.
        # Fail it here, where we still know why.
        logger.error(
            "asr.enqueue_failed",
            extra={
                "job_id": str(job_id),
                "error": str(exc),
                "error_class": type(exc).__name__,
            },
        )
        async with tenant_connection(state.app_pool, claims.tid) as conn:
            await repository.fail_job(
                conn,
                job_id=job_id,
                error_kind=str(JobErrorKind.ENQUEUE_FAILED),
                error_detail=f"{type(exc).__name__}: {exc}",
            )
        _uploads_counter.add(1, {"status": "rejected"})
        _jobs_counter.add(1, {"status": "failed"})
        http_exc = HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "the job was recorded but could not be queued; it has been "
                "marked failed. Submit the recording again."
            ),
        )
        http_exc.problem_extras = {  # type: ignore[attr-defined]
            "type_uri": f"urn:mdx:asr:job:{JobErrorKind.ENQUEUE_FAILED}",
            "code": str(JobErrorKind.ENQUEUE_FAILED),
            "reason": "transcription queue unavailable",
            "job_id": str(job_id),
        }
        raise http_exc from exc

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
    "/jobs/{job_id}/result",
    response_model=TranscriptResultView,
    summary="Fetch a completed job's transcript (409 if not ready).",
)
async def get_job_result(
    job_id: UUID,
    request: Request,
    claims: Annotated[Claims, Depends(requires("asr.read", "asr_job"))] = ...,  # type: ignore[assignment]
) -> TranscriptResultView:
    """Architecture rule: presigned URLs serve ciphertext — useless to a
    browser — so the transcript is decrypted through the envelope path and
    returned on this AUTHENTICATED endpoint (ADR-0011 forbids client-side
    decrypt). Every plaintext read is audited.

    The raw transcript is run through nlp-service's batch pipeline
    (dictated «крапка»→"." + punctuation/number normalization) with the
    caller's own bearer forwarded; on any NLP failure the raw transcript
    is returned with ``nlp_applied=false``."""
    state = get_state()
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        view = await repository.get_job(conn, job_id=job_id)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if view.status != JobStatus.COMPLETE:
        # RFC 9457 problem detail — the result isn't ready (still queued/running)
        # or never will be (failed/cancelled). The client polls status and
        # retries; see spec §2.5 + retro E10 (FE retry-on-403-then-refetch).
        exc = HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"job {job_id} is in status {view.status.value!r}, not 'complete'",
        )
        # For a terminal status the failure vocabulary travels with the
        # 409, so a client polling for a transcript learns in one response
        # that it is not coming and whether resubmitting would help —
        # rather than polling a `failed` job until it gives up.
        exc.problem_extras = {  # type: ignore[attr-defined]
            "type_uri": "urn:mdx:asr:result:not-ready",
            "reason": "Transcription result is not ready",
            "job_status": view.status.value,
            "error_kind": view.error_kind,
            "error_stage": view.error_stage,
            "error_retryable": view.error_retryable,
            "error_message": view.error_message,
        }
        raise exc
    try:
        raw = await state.transcript_store.get(
            key=f"{claims.tid}/{job_id}.json.enc",
            tenant_id=claims.tid,
            aad=job_id.bytes,
        )
    except ObjectNotFoundError:
        # Job says complete but the ciphertext is gone — retention TTL or
        # the S11 erasure engine removed it after the row was written.
        gone = HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                f"job {job_id} is complete but its transcript object "
                "has been deleted (retention/erasure)"
            ),
        )
        gone.problem_extras = {  # type: ignore[attr-defined]
            "type_uri": "urn:mdx:asr:result:erased",
            "reason": "Transcription result no longer exists",
            "code": "transcript_erased",
        }
        raise gone from None
    output = TranscriptionOutput.model_validate_json(raw)
    await state.audit_writer.write_event(
        tenant_id=claims.tid,
        kind=audit_kinds.TRANSCRIPT_ACCESSED,
        actor_sub=claims.sub,
        actor_role=(claims.roles[0] if claims.roles else None),
        target_kind="asr_job",
        target_id=str(job_id),
        payload={"audio_id": str(view.audio_id), "bytes": len(raw)},
        severity=Severity.INFO,
    )
    return await _enriched_result_view(
        state,
        job_id=job_id,
        output=output,
        authorization=request.headers.get("authorization"),
    )


_PUNCT_ONLY = frozenset(".,:;!?…—–-()[]{}«»“”‘’'\"/\\*#№%&@+−=_|")


async def _enriched_result_view(
    state: object,
    *,
    job_id: UUID,
    output: TranscriptionOutput,
    authorization: str | None,
) -> TranscriptResultView:
    """Run the raw transcript through nlp-service; fall back to raw on failure."""
    raw_segments = [
        EnrichedSegment(
            text=s.text,
            raw_text=s.text,
            start_ms=s.start_ms,
            end_ms=s.end_ms,
            words=s.words,
            avg_confidence=s.avg_confidence,
        )
        for s in output.segments
    ]
    view = TranscriptResultView(
        job_id=job_id,
        language=output.language,
        segments=raw_segments,
        metadata=output.metadata,
    )
    if not settings.nlp_enrich_enabled or not output.segments:
        return view

    payload = [
        {
            "text": s.text,
            "words": [
                {
                    "text": w.text,
                    "start_s": w.start_ms / 1000.0,
                    "end_s": w.end_ms / 1000.0,
                    "probability": w.probability,
                }
                for w in s.words
            ],
        }
        for s in output.segments
    ]
    resp = await state.nlp_client.process_segments(  # type: ignore[attr-defined]
        tenant_id=UUID(int=0),  # tenant comes from the forwarded bearer
        segments=payload,
        language=output.language,
        authorization=authorization,
    )
    if resp is None or len(resp.get("segments", [])) != len(output.segments):
        return view  # NLP down/mismatched — serve the raw transcript

    enriched: list[EnrichedSegment] = []
    for raw_seg, nlp_seg in zip(output.segments, resp["segments"], strict=True):
        text = str(nlp_seg.get("text", "")).strip()
        spans = [
            ConfidenceSpanView(
                start_char=sp["start_char"],
                end_char=sp["end_char"],
                level=sp["level"],
            )
            for sp in nlp_seg.get("confidence_spans", [])
        ]
        # A segment that was PURELY a voice command («новий абзац» alone)
        # comes back empty — it has no textual rendering; drop it.
        if not text:
            continue
        # A segment that is ONLY punctuation (Whisper split a dictated
        # «Крапка» into its own segment) merges into the previous one.
        # No-op when the previous segment already ends with that mark —
        # the punctuation stage adds trailing periods on its own.
        if enriched and all(ch in _PUNCT_ONLY for ch in text):
            prev = enriched[-1]
            merged = prev.text.rstrip()
            if not merged.endswith(text):
                merged += text
            enriched[-1] = prev.model_copy(
                update={"text": merged, "end_ms": raw_seg.end_ms}
            )
            continue
        enriched.append(
            EnrichedSegment(
                text=text,
                raw_text=raw_seg.text,
                start_ms=raw_seg.start_ms,
                end_ms=raw_seg.end_ms,
                words=raw_seg.words,
                avg_confidence=raw_seg.avg_confidence,
                confidence_spans=spans,
            )
        )
    return view.model_copy(
        update={
            "segments": enriched,
            "nlp_applied": True,
            "nlp_pipeline_version": resp.get("pipeline_version"),
        }
    )


@router.get(
    "/jobs",
    response_model=list[TranscriptionJobView],
    summary="List tenant's recent jobs.",
)
async def list_jobs(
    claims: Annotated[
        Claims,
        Depends(requires_any(("asr.read", "asr_job"), ("stats.read", "tenant"))),
    ],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    since: Annotated[datetime | None, Query()] = None,
) -> list[TranscriptionJobView]:
    """S14 — two standings reach this list.

    `asr.read` (clinician / nurse) gets the clinical list, and since S14
    each row names the patient whose recording it is. `stats.read`
    (tenant_admin, who holds no clinical read) gets the same rows with
    every patient reference and every pointer AT the audio stripped:
    enough to count jobs and chart throughput on the business dashboard,
    not enough to identify or open anything.
    """
    state = get_state()
    clinical = can_claims(claims, "asr.read", "asr_job")
    async with tenant_connection(state.app_pool, claims.tid) as conn:
        jobs = await repository.list_jobs(
            conn, limit=limit, status=status_filter, since=since
        )
    if clinical:
        return jobs
    return [
        job.model_copy(
            update={
                "patient_id": None,
                "patient_name_uk": None,
                "patient_name_en": None,
                # A presigned result URL is a door to the transcript, and
                # `error_detail` is free text built from an exception that
                # may quote the audio it choked on.
                "result_url": None,
                "error_detail": None,
            }
        )
        for job in jobs
    ]


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
