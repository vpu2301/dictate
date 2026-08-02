"""Audio-replay domain: source resolution, segment listing, clip registry,
download tokens (sprint 15, ADR-0037).

Clip URLs are NOT S3 presigns — presigned URLs serve envelope ciphertext
(platform rule 3 / ADR-0011), useless to an ``<audio>`` element. The
sanctioned shape is the DSAR download-token idiom (ADR-0028): an
HMAC-signed 5-minute token bound to (tenant, clip), redeemed at an
authenticated decrypt-and-stream endpoint. The clip registry lives in
Redis with the same TTL — clips are ephemeral derivatives of PHI, never
a second permanent copy (bucket ILM is the backstop, not the mechanism).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

_TOKEN_DOMAIN = "mdx-audio-clip-v1"
_REGISTRY_PREFIX = "audio-clip:"


# ── Download tokens (core-service download_tokens.py idiom) ─────────


def _sig(key_hex: str, tenant_id: UUID, clip_id: UUID, exp_unix: int) -> str:
    msg = f"{_TOKEN_DOMAIN}:{tenant_id}:{clip_id}:{exp_unix}".encode("ascii")
    return hmac.new(bytes.fromhex(key_hex), msg, hashlib.sha256).hexdigest()


def mint_clip_token(
    key_hex: str, *, tenant_id: UUID, clip_id: UUID, ttl_seconds: int
) -> tuple[str, int]:
    """Return ``(token, exp_unix)`` valid for ``ttl_seconds`` from now."""
    exp_unix = int(time.time()) + ttl_seconds
    return f"{exp_unix}.{_sig(key_hex, tenant_id, clip_id, exp_unix)}", exp_unix


def verify_clip_token(key_hex: str, *, tenant_id: UUID, clip_id: UUID, token: str) -> bool:
    exp_part, sep, mac_part = token.partition(".")
    if not sep or not exp_part.isdigit():
        return False
    if int(exp_part) < time.time():
        return False
    return hmac.compare_digest(mac_part, _sig(key_hex, tenant_id, clip_id, int(exp_part)))


# ── Clip registry (Redis, TTL == token TTL) ─────────────────────────


async def register_clip(
    redis: Any, *, clip_id: UUID, tenant_id: UUID, report_id: UUID,
    object_key: str, ttl_seconds: int,
) -> None:
    payload = json.dumps(
        {"key": object_key, "tenant_id": str(tenant_id), "report_id": str(report_id)}
    )
    await redis.setex(f"{_REGISTRY_PREFIX}{clip_id}", ttl_seconds, payload)


async def lookup_clip(redis: Any, *, clip_id: UUID) -> dict[str, str] | None:
    raw = await redis.get(f"{_REGISTRY_PREFIX}{clip_id}")
    if raw is None:
        return None
    decoded: dict[str, str] = json.loads(raw)
    return decoded


# ── Audio source resolution (the 410 taxonomy) ──────────────────────


@dataclass(slots=True, frozen=True)
class AudioSource:
    kind: str  # 'session' | 'batch'
    object_key: str
    aad: bytes
    mime_type: str
    # Session-relative ms of the FIRST retained byte: 0 normally; positive
    # when the tmpfs ring wrapped (truncated session) and early audio is
    # permanently gone. Slice offsets subtract this.
    retained_from_ms: int
    duration_ms: int | None


class AudioUnavailableError(Exception):
    """Maps to 410 + problem code — the endpoint's honesty contract."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _object_key_from_uri(storage_uri: str) -> str:
    """`minio://bucket/tenant/id.enc` → `tenant/id.enc` (erasers.py idiom)."""
    without_scheme = storage_uri.split("://", 1)[1]
    return without_scheme.split("/", 1)[1]


async def resolve_audio_source(
    conn: asyncpg.Connection, *, report_id: UUID
) -> AudioSource:
    """Walk report → dictation session (or batch job) → audio_files.

    Raises AudioUnavailableError with the honest reason at every dead end:
      * no_audio_source        — report was never dictated from audio
      * audio_not_retained     — never stored (object store disabled / demo purge)
      * audio_erased           — retention/erasure removed it
    ``audio_partially_retained`` is raised later, at slice time, when the
    requested range predates a truncated session's surviving window.
    """
    refs = await conn.fetchrow(
        "SELECT source_session_id, source_asr_job_id FROM reports WHERE id = $1",
        report_id,
    )
    if refs is None or (refs["source_session_id"] is None and refs["source_asr_job_id"] is None):
        raise AudioUnavailableError("no_audio_source", "this report has no source recording")

    if refs["source_session_id"] is not None:
        session = await conn.fetchrow(
            "SELECT audio_file_id, truncated, total_audio_ms "
            "FROM dictation_sessions WHERE id = $1",
            refs["source_session_id"],
        )
        if session is None:
            raise AudioUnavailableError(
                "audio_erased", "the source dictation session has been erased"
            )
        if session["audio_file_id"] is None:
            raise AudioUnavailableError(
                "audio_not_retained",
                "no recording was retained for this session "
                "(store disabled or purge-on-finalize)",
            )
        audio = await conn.fetchrow(
            "SELECT storage_uri, mime_type, duration_ms, status "
            "FROM audio_files WHERE id = $1",
            session["audio_file_id"],
        )
        if audio is None or audio["status"] == "deleted":
            raise AudioUnavailableError(
                "audio_erased", "the recording has been deleted (retention/erasure)"
            )
        retained_from_ms = 0
        if session["truncated"] and audio["duration_ms"] is not None:
            # Ring wrapped: only the LAST duration_ms of the session
            # survived; everything before it is permanently gone.
            retained_from_ms = max(
                0, int(session["total_audio_ms"]) - int(audio["duration_ms"])
            )
        return AudioSource(
            kind="session",
            object_key=_object_key_from_uri(audio["storage_uri"]),
            aad=refs["source_session_id"].bytes,
            mime_type=audio["mime_type"],
            retained_from_ms=retained_from_ms,
            duration_ms=audio["duration_ms"],
        )

    job = await conn.fetchrow(
        "SELECT audio_id FROM transcription_jobs WHERE id = $1",
        refs["source_asr_job_id"],
    )
    if job is None:
        raise AudioUnavailableError("audio_erased", "the source transcription job has been erased")
    audio = await conn.fetchrow(
        "SELECT id, storage_uri, mime_type, duration_ms, status "
        "FROM audio_files WHERE id = $1",
        job["audio_id"],
    )
    if audio is None or audio["status"] == "deleted":
        raise AudioUnavailableError(
            "audio_erased", "the recording has been deleted (retention/erasure)"
        )
    return AudioSource(
        kind="batch",
        object_key=_object_key_from_uri(audio["storage_uri"]),
        aad=audio["id"].bytes,
        mime_type=audio["mime_type"],
        retained_from_ms=0,
        duration_ms=audio["duration_ms"],
    )


# ── Segment listing ─────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class SegmentRef:
    segment_id: UUID | None  # real uuid for conversation-mode segments; else None
    index: int
    start_ms: int
    end_ms: int
    speaker: str | None
    speaker_role: str | None


def _decode_transcript(raw: Any) -> list[dict[str, Any]]:
    """transcript_jsonb reads back as a JSON STRING (no jsonb codec on the
    pool) — the dictation-service ``_transcript_from_row`` defensive shape."""
    if raw is None:
        return []
    if isinstance(raw, str):
        decoded: Any = json.loads(raw)
        return decoded if isinstance(decoded, list) else []
    return raw if isinstance(raw, list) else []


def segments_from_transcript(
    transcript: list[dict[str, Any]], *, segment_ids: list[UUID]
) -> list[SegmentRef]:
    """Map a session transcript to replay segments.

    When the section carries ``transcript_segment_ids`` (sprint-14
    conversation drafts), only those segments return. Otherwise — nearly
    every report today, the field being a sprint-08 placeholder — the
    WHOLE session transcript returns so replay still works; the FE aligns
    by timing. Dictation-mode segments have no ``id`` (deliberately not
    minted here: the finalize path commits to a byte-identical legacy
    shape) → ``segment_id`` is null and ``index`` addresses them.
    """
    wanted = {str(s) for s in segment_ids}
    refs: list[SegmentRef] = []
    for idx, seg in enumerate(transcript):
        seg_id = seg.get("id")
        if wanted and (seg_id is None or seg_id not in wanted):
            continue
        try:
            start_ms, end_ms = int(seg["start_ms"]), int(seg["end_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        refs.append(
            SegmentRef(
                segment_id=UUID(seg_id) if seg_id else None,
                index=idx,
                start_ms=start_ms,
                end_ms=end_ms,
                speaker=seg.get("speaker"),
                speaker_role=seg.get("speaker_role"),
            )
        )
    return refs


async def load_session_transcript(
    conn: asyncpg.Connection, *, session_id: UUID
) -> list[dict[str, Any]]:
    raw = await conn.fetchval(
        "SELECT transcript_jsonb FROM dictation_sessions WHERE id = $1", session_id
    )
    return _decode_transcript(raw)
