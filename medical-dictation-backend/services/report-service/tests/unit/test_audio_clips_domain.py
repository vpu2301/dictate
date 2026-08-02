"""Audio replay domain — tokens, registry, 410 taxonomy, segment mapping."""

from __future__ import annotations

import json
import time
from uuid import uuid4

import pytest
from fakeredis.aioredis import FakeRedis

from report_service.domain import audio_clips as clips

KEY_HEX = "aa" * 32
TENANT = uuid4()


# ── download tokens ─────────────────────────────────────────────────


def test_token_roundtrip():
    clip_id = uuid4()
    token, exp = clips.mint_clip_token(
        KEY_HEX, tenant_id=TENANT, clip_id=clip_id, ttl_seconds=300
    )
    assert exp > time.time()
    assert clips.verify_clip_token(KEY_HEX, tenant_id=TENANT, clip_id=clip_id, token=token)


def test_token_bound_to_tenant_and_clip():
    clip_id = uuid4()
    token, _ = clips.mint_clip_token(
        KEY_HEX, tenant_id=TENANT, clip_id=clip_id, ttl_seconds=300
    )
    assert not clips.verify_clip_token(
        KEY_HEX, tenant_id=uuid4(), clip_id=clip_id, token=token
    )
    assert not clips.verify_clip_token(
        KEY_HEX, tenant_id=TENANT, clip_id=uuid4(), token=token
    )


def test_expired_token_rejected():
    clip_id = uuid4()
    token, _ = clips.mint_clip_token(
        KEY_HEX, tenant_id=TENANT, clip_id=clip_id, ttl_seconds=-1
    )
    assert not clips.verify_clip_token(
        KEY_HEX, tenant_id=TENANT, clip_id=clip_id, token=token
    )


def test_tampered_token_rejected():
    clip_id = uuid4()
    token, _ = clips.mint_clip_token(
        KEY_HEX, tenant_id=TENANT, clip_id=clip_id, ttl_seconds=300
    )
    exp, _, mac = token.partition(".")
    assert not clips.verify_clip_token(
        KEY_HEX, tenant_id=TENANT, clip_id=clip_id, token=f"{exp}.{'0' * len(mac)}"
    )
    assert not clips.verify_clip_token(
        KEY_HEX, tenant_id=TENANT, clip_id=clip_id, token="garbage"
    )


# ── registry ────────────────────────────────────────────────────────


async def test_registry_roundtrip_and_ttl():
    redis = FakeRedis()
    clip_id = uuid4()
    await clips.register_clip(
        redis, clip_id=clip_id, tenant_id=TENANT, report_id=uuid4(),
        object_key=f"clips/{TENANT}/{clip_id}.ogg.enc", ttl_seconds=300,
    )
    entry = await clips.lookup_clip(redis, clip_id=clip_id)
    assert entry is not None
    assert entry["tenant_id"] == str(TENANT)
    assert entry["key"].endswith(".ogg.enc")
    ttl = await redis.ttl(f"audio-clip:{clip_id}")
    assert 0 < ttl <= 300
    assert await clips.lookup_clip(redis, clip_id=uuid4()) is None


# ── 410 taxonomy (fake conn keyed on SQL substrings) ────────────────


class _FakeConn:
    def __init__(self, rows: dict[str, dict | None]) -> None:
        self._rows = rows

    async def fetchrow(self, sql: str, *args):
        for marker, row in self._rows.items():
            if marker in sql:
                return row
        raise AssertionError(f"unexpected SQL: {sql}")


async def test_no_audio_source():
    conn = _FakeConn({"FROM reports": {"source_session_id": None, "source_asr_job_id": None}})
    with pytest.raises(clips.AudioUnavailableError) as exc:
        await clips.resolve_audio_source(conn, report_id=uuid4())
    assert exc.value.code == "no_audio_source"


async def test_session_audio_never_stored():
    sid = uuid4()
    conn = _FakeConn({
        "FROM reports": {"source_session_id": sid, "source_asr_job_id": None},
        "FROM dictation_sessions": {
            "audio_file_id": None, "truncated": False, "total_audio_ms": 10_000
        },
    })
    with pytest.raises(clips.AudioUnavailableError) as exc:
        await clips.resolve_audio_source(conn, report_id=uuid4())
    assert exc.value.code == "audio_not_retained"


async def test_session_audio_erased():
    sid = uuid4()
    conn = _FakeConn({
        "FROM reports": {"source_session_id": sid, "source_asr_job_id": None},
        "FROM dictation_sessions": {
            "audio_file_id": uuid4(), "truncated": False, "total_audio_ms": 10_000
        },
        "FROM audio_files": None,
    })
    with pytest.raises(clips.AudioUnavailableError) as exc:
        await clips.resolve_audio_source(conn, report_id=uuid4())
    assert exc.value.code == "audio_erased"


async def test_truncated_session_reports_retained_offset():
    sid = uuid4()
    conn = _FakeConn({
        "FROM reports": {"source_session_id": sid, "source_asr_job_id": None},
        "FROM dictation_sessions": {
            "audio_file_id": uuid4(), "truncated": True, "total_audio_ms": 600_000
        },
        "FROM audio_files": {
            "storage_uri": f"minio://mdx-audio/dictations/{TENANT}/{sid}.wav.enc",
            "mime_type": "audio/wav", "duration_ms": 120_000, "status": "stored",
        },
    })
    source = await clips.resolve_audio_source(conn, report_id=uuid4())
    assert source.kind == "session"
    assert source.retained_from_ms == 480_000  # only the last 2 min survived
    assert source.object_key == f"dictations/{TENANT}/{sid}.wav.enc"
    assert source.aad == sid.bytes


async def test_batch_source_resolves_via_job():
    job_id, audio_id = uuid4(), uuid4()
    conn = _FakeConn({
        "FROM reports": {"source_session_id": None, "source_asr_job_id": job_id},
        "FROM transcription_jobs": {"audio_id": audio_id},
        "FROM audio_files": {
            "id": audio_id,
            "storage_uri": f"minio://mdx-audio/{TENANT}/{audio_id}.enc",
            "mime_type": "audio/mpeg", "duration_ms": 90_000, "status": "stored",
        },
    })
    source = await clips.resolve_audio_source(conn, report_id=uuid4())
    assert source.kind == "batch"
    assert source.aad == audio_id.bytes
    assert source.retained_from_ms == 0


# ── segment mapping ─────────────────────────────────────────────────

_CONVO_TRANSCRIPT = [
    {"id": "0be2f150-0000-4000-8000-000000000001", "text": "Добрий день",
     "start_ms": 0, "end_ms": 1500, "speaker": "SPEAKER_00", "speaker_role": "doctor"},
    {"id": "0be2f150-0000-4000-8000-000000000002", "text": "Болить голова",
     "start_ms": 1600, "end_ms": 4200, "speaker": "SPEAKER_01", "speaker_role": "patient"},
]

_DICTATION_TRANSCRIPT = [
    {"text": "Пацієнт скаржиться", "start_ms": 0, "end_ms": 2000, "avg_confidence": 0.9},
    {"text": "на біль у грудях", "start_ms": 2100, "end_ms": 4000, "avg_confidence": 0.9},
]


def test_conversation_segments_mapped_by_id():
    from uuid import UUID

    wanted = [UUID("0be2f150-0000-4000-8000-000000000002")]
    refs = clips.segments_from_transcript(_CONVO_TRANSCRIPT, segment_ids=wanted)
    assert len(refs) == 1
    assert refs[0].segment_id == wanted[0]
    assert refs[0].speaker_role == "patient"
    assert (refs[0].start_ms, refs[0].end_ms) == (1600, 4200)


def test_dictation_segments_fall_back_to_whole_transcript():
    refs = clips.segments_from_transcript(_DICTATION_TRANSCRIPT, segment_ids=[])
    assert len(refs) == 2
    assert refs[0].segment_id is None and refs[0].index == 0
    assert refs[1].speaker is None


def test_jsonb_string_decoding():
    raw = json.dumps(_CONVO_TRANSCRIPT)
    decoded = clips._decode_transcript(raw)
    assert len(decoded) == 2
    assert clips._decode_transcript(None) == []
    assert clips._decode_transcript("{}") == []
