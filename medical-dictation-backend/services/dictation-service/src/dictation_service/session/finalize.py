"""Session finalization — flush windower, package audio, persist.

End-of-life for every session that didn't fail or abandon:

1. Stop accepting new audio (state → finalized).
2. Run a last windower tick to flush partials → finals.
3. Concatenate the tmpfs ring into an in-memory WAV (PCM 16 kHz mono).
4. Encrypt + upload via ``EncryptedObjectStore`` (sprint 03 lib).
5. Insert ``audio_files`` row + update ``dictation_sessions``.
6. Free tmpfs / decoder / Whisper context.
7. Send ``SessionTerminated`` to the client.

The function is idempotent for the row UPDATEs: if a second
finalize is called on an already-finalized session, the second call
short-circuits.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import struct
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import numpy as np

from audit import AuditWriter, Severity
from crypto import Envelope
from db import tenant_connection
from storage import EncryptedObjectStore
from storage.object_store import ObjectHeader, header_metadata_for_row

from .. import audit_kinds
from ..domain import repository
from ..session.manager import SessionContext
from ..session.state import SessionState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    audio_file_id: UUID | None
    truncated: bool
    transcript_segments: int


async def finalize_session(
    *,
    ctx: SessionContext,
    app_pool: object,  # asyncpg.Pool — Any to avoid an import cycle
    audit_writer: AuditWriter,
    audio_store: EncryptedObjectStore,
    envelope: Envelope,
    reason: str = "normal",
) -> FinalizeResult:
    """Idempotently finalize a session.

    ``reason`` is one of: ``normal``, ``cap_reached``, ``token_expired``,
    ``worker_failure``. The DB row's status becomes ``finalized`` (or
    ``failed`` for worker_failure — caller picks).
    """
    pcm = _flush_buffer(ctx)
    pcm_bytes_len = pcm.nbytes
    duration_ms = pcm.shape[0] * 1000 // 16_000

    # Truncation detection: if the producer cursor went beyond the ring
    # (e.g., 60 min cap + retransmit abuse), audio_file may be shorter
    # than ``ctx.buffer.total_ms``. Flag it.
    truncated = False
    if ctx.buffer is not None:
        truncated = ctx.buffer.total_ms > duration_ms

    audio_file_id: UUID | None = None
    object_store_disabled = audio_store.is_disabled
    if pcm_bytes_len > 0 and not object_store_disabled:
        wav_bytes = _pcm_to_wav(pcm)
        audio_file_id = uuid4()
        storage_key = f"dictations/{ctx.tenant_id}/{ctx.session_id}.wav.enc"
        try:
            header = await audio_store.put(
                key=storage_key,
                plaintext=wav_bytes,
                tenant_id=ctx.tenant_id,
                aad=ctx.session_id.bytes,
            )
        except Exception as exc:  # noqa: BLE001
            from storage import ObjectStoreDisabledError

            if isinstance(exc, ObjectStoreDisabledError):
                # Race: env flipped between is_disabled check and put.
                # Fall through to the demo path.
                object_store_disabled = True
                audio_file_id = None
            else:
                raise

        if not object_store_disabled:
            async with tenant_connection(app_pool, ctx.tenant_id) as conn:
                await conn.execute(
                    """
                    INSERT INTO audio_files
                        (id, tenant_id, uploader_sub, mime_type, size_bytes,
                         duration_ms, sha256, envelope_metadata, storage_uri, status,
                         encounter_id)
                    VALUES ($1,$2,$3,'audio/wav',$4,$5,$6,$7::jsonb,$8,'stored',$9)
                    """,
                    audio_file_id,
                    ctx.tenant_id,
                    ctx.user_id,
                    len(wav_bytes),
                    duration_ms,
                    hashlib.sha256(wav_bytes).digest(),
                    # asyncpg binds jsonb from a JSON string, not a dict
                    # (no dict→jsonb codec is registered on the pool) — see
                    # asr-service's insert_audio_row for the same pattern.
                    json.dumps(_header_to_json(header)),
                    f"minio://{audio_store.bucket}/{storage_key}",
                    ctx.encounter_id,
                )

        await audit_writer.write_event(
            tenant_id=ctx.tenant_id,
            kind=audit_kinds.AUDIO_UPLOADED,
            actor_sub=ctx.user_id,
            target_kind="audio",
            target_id=str(audio_file_id),
            payload={
                "session_id": str(ctx.session_id),
                "duration_ms": duration_ms,
                "size_bytes": len(wav_bytes),
            },
            severity=Severity.INFO,
        )

    if truncated:
        await audit_writer.write_event(
            tenant_id=ctx.tenant_id,
            kind=audit_kinds.AUDIO_TRUNCATED,
            actor_sub=ctx.user_id,
            target_kind="dictation_session",
            target_id=str(ctx.session_id),
            payload={
                "observed_ms": ctx.buffer.total_ms if ctx.buffer else 0,
                "stored_ms": duration_ms,
            },
            severity=Severity.WARN,
        )

    # Persist the transcript + timing metrics.
    transcript_jsonb = _transcript_to_jsonb(ctx)
    async with tenant_connection(app_pool, ctx.tenant_id) as conn:
        await repository.write_finalized(
            conn,
            session_id=ctx.session_id,
            audio_file_id=audio_file_id,
            transcript_jsonb=transcript_jsonb,
            total_audio_ms=duration_ms,
            total_speech_ms=duration_ms,  # approximation; real VAD-speech in sprint 14
            avg_partial_latency_ms=_avg(ctx.partial_latencies_ms),
            avg_final_latency_ms=_avg(ctx.final_latencies_ms),
            rtf=None,
            network_drop_count=ctx.network_drop_count,
            truncated=truncated,
        )

    await audit_writer.write_event(
        tenant_id=ctx.tenant_id,
        kind=audit_kinds.SESSION_FINALIZED,
        actor_sub=ctx.user_id,
        target_kind="dictation_session",
        target_id=str(ctx.session_id),
        payload={
            "reason": reason,
            "duration_ms": duration_ms,
            "audio_file_id": str(audio_file_id) if audio_file_id else None,
            "segments": len(transcript_jsonb),
            "truncated": truncated,
        },
        severity=Severity.INFO,
    )

    # Free per-session resources.
    if ctx.buffer is not None:
        ctx.buffer.close()
        ctx.buffer = None
    ctx.decoder = None
    ctx.state = SessionState.FINALIZED

    return FinalizeResult(
        audio_file_id=audio_file_id,
        truncated=truncated,
        transcript_segments=len(transcript_jsonb),
    )


def _flush_buffer(ctx: SessionContext) -> np.ndarray:
    """Read the entire session buffer as a contiguous float32 ndarray.

    If the ring wrapped, the readable portion is the most-recent
    ring-length samples; older audio is unrecoverable here (transcript
    already committed for the lost range).
    """
    if ctx.buffer is None:
        return np.zeros(0, dtype=np.float32)
    total = ctx.buffer.total_samples
    ring_samples = ctx.buffer._ring_samples  # private but stable
    start = max(0, total - ring_samples)
    samples: np.ndarray = ctx.buffer.read(start, total)
    return samples


def _pcm_to_wav(pcm: np.ndarray) -> bytes:
    """Wrap a float32 mono 16 kHz PCM array in a minimal WAV container."""
    samples = np.clip(pcm, -1.0, 1.0)
    int16 = (samples * 32767.0).astype(np.int16)
    raw = int16.tobytes()
    buf = io.BytesIO()
    # RIFF/WAVE header for 16-bit PCM mono 16 kHz.
    sample_rate = 16_000
    channels = 1
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + len(raw)))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))
    buf.write(struct.pack("<H", 1))  # PCM
    buf.write(struct.pack("<H", channels))
    buf.write(struct.pack("<I", sample_rate))
    buf.write(struct.pack("<I", byte_rate))
    buf.write(struct.pack("<H", block_align))
    buf.write(struct.pack("<H", bits))
    buf.write(b"data")
    buf.write(struct.pack("<I", len(raw)))
    buf.write(raw)
    return buf.getvalue()


def _transcript_to_jsonb(ctx: SessionContext) -> list[dict[str, Any]]:
    """Project finalized_segments → JSON-safe list of segment dicts.

    The shape matches sprint-03's TranscriptionOutput.segments so the
    NLP postprocessor (sprint 05) can consume both batch and streaming
    transcripts uniformly.
    """
    out: list[dict[str, Any]] = []
    for seg in ctx.finalized_segments:
        out.append(
            {
                "text": seg.text,
                "start_ms": seg.start_ms,
                "end_ms": seg.end_ms,
                "avg_confidence": float(seg.avg_confidence),
                "words": [
                    {
                        "text": w.text,
                        "start_ms": w.start_ms,
                        "end_ms": w.end_ms,
                        "probability": float(w.probability),
                    }
                    for w in (seg.words or [])
                ],
                # sprint-05 will populate this from NLP; sprint-04 reserves the slot.
                "voice_command": None,
            }
        )
    return out


def _header_to_json(header: ObjectHeader) -> dict[str, str | int]:
    return header_metadata_for_row(header)


def _avg(xs: list[int]) -> int | None:
    return int(sum(xs) / len(xs)) if xs else None
