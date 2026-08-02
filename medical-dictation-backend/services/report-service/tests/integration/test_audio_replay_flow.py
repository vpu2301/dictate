"""Sprint-15 audio replay — end-to-end pipeline against live infra.

Needs ``RUN_DB_INTEGRATION=1`` + ``make dev-up && make migrate-up && make seed``
(Postgres, MinIO, the dev master key). Proves the whole chain the POST
endpoint drives: session row + encrypted WAV → resolve → full GCM decrypt
(the ONLY read path — no range mode exists) → ms slice (+pad) → opus →
encrypted clip object → token stream-back, with the slice verified by
checksum against an independently computed reference.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import wave
from uuid import UUID, uuid4

import asyncpg
import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RUN_DB_INTEGRATION") != "1",
        reason="set RUN_DB_INTEGRATION=1; needs dev-up + migrate-up + seed + MinIO",
    ),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
DB_NAME = os.environ.get("POSTGRES_DB", "medical_dictation")
SU_DSN = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
APP_DSN = f"postgresql://app_role:app_role@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
CRYPTO_DSN = (
    f"postgresql://crypto_writer:crypto_writer@{POSTGRES_HOST}:{POSTGRES_PORT}/{DB_NAME}"
)
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:9000")
MASTER_KEY = os.environ.get(
    "MDX_MASTER_KEY_PATH",
    os.path.join(os.path.dirname(__file__), "../../../..", "infra/dev/master.key"),
)

TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")


def _make_wav(duration_ms: int) -> tuple[bytes, bytes]:
    n = 16_000 * duration_ms // 1000
    pcm = b"".join(struct.pack("<h", i % 32768) for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(pcm)
    return buf.getvalue(), pcm


async def _stores():
    from crypto import Envelope, FileMasterKeyProvider, TenantKekRepository
    from db import create_pool
    from storage import EncryptedObjectStore, S3Client

    crypto_pool = await create_pool(
        CRYPTO_DSN, application_name="itest-replay-crypto", min_size=1, max_size=2
    )
    master = FileMasterKeyProvider(path=os.path.abspath(MASTER_KEY))
    await master.startup_self_check()
    kek_repo = TenantKekRepository(pool=crypto_pool, master_key_provider=master)
    envelope = Envelope(master_key_provider=master, kek_repository=kek_repo)
    s3 = S3Client(
        endpoint_url=S3_ENDPOINT, access_key="minioadmin", secret_key="minioadmin",
        region="us-east-1", use_ssl=False,
    )
    audio = EncryptedObjectStore(s3=s3, bucket="mdx-audio", envelope=envelope)
    clips_store = EncryptedObjectStore(s3=s3, bucket="mdx-audio-clips", envelope=envelope)
    return crypto_pool, audio, clips_store


async def test_full_replay_pipeline_with_checksum_and_diarized_segments():
    from report_service.domain import audio_clips as clips
    from report_service.domain import audio_slicer

    session_id, audio_id, report_id = uuid4(), uuid4(), uuid4()
    wav, pcm = _make_wav(10_000)
    object_key = f"dictations/{TENANT_A}/{session_id}.wav.enc"

    # Sprint-14 conversation transcript shape: doctor + patient turns.
    seg_doctor, seg_patient = uuid4(), uuid4()
    transcript = [
        {"id": str(seg_doctor), "text": "Що вас турбує?", "start_ms": 0, "end_ms": 1900,
         "avg_confidence": 0.93, "speaker": "SPEAKER_00", "speaker_role": "doctor",
         "words": [], "voice_command": None},
        {"id": str(seg_patient), "text": "Болить голова вже три дні", "start_ms": 2000,
         "end_ms": 4500, "avg_confidence": 0.91, "speaker": "SPEAKER_01",
         "speaker_role": "patient", "words": [], "voice_command": None},
    ]

    su = await asyncpg.connect(SU_DSN)
    crypto_pool, audio_store, clips_store = await _stores()
    try:
        await audio_store.put(
            key=object_key, plaintext=wav, tenant_id=TENANT_A, aad=session_id.bytes
        )
        clinician = await su.fetchval(
            "SELECT sub FROM users WHERE tenant_id=$1 LIMIT 1", TENANT_A
        )
        prompt_id = await su.fetchval("SELECT id FROM medical_prompts LIMIT 1")
        assert clinician is not None and prompt_id is not None, "run `make seed` first"

        await su.execute(
            "INSERT INTO audio_files (id, tenant_id, uploader_sub, mime_type, size_bytes,"
            " sha256, envelope_metadata, storage_uri, status, duration_ms)"
            " VALUES ($1,$2,$3,'audio/wav',$4,$5,'{}',$6,'stored',$7)",
            audio_id, TENANT_A, clinician, len(wav), hashlib.sha256(wav).digest(),
            f"minio://mdx-audio/{object_key}", 10_000,
        )
        await su.execute(
            "INSERT INTO dictation_sessions (id, tenant_id, user_id, language, prompt_id,"
            " status, transcript_jsonb, audio_file_id, total_audio_ms)"
            " VALUES ($1,$2,$3,'uk',$4,'finalized',$5,$6,10000)",
            session_id, TENANT_A, clinician, prompt_id, json.dumps(transcript), audio_id,
        )

        # ── resolve → decrypt → slice(+pad) → checksum vs reference ──
        app_conn = await asyncpg.connect(APP_DSN)
        try:
            await app_conn.execute(
                "SELECT set_config('app.tenant_id', $1, false)", str(TENANT_A)
            )
            # Minimal reports row surrogate: resolve reads only the two
            # source columns, so exercise it directly via the session ref.
            refs_row = {"source_session_id": session_id, "source_asr_job_id": None}

            class _Conn:
                async def fetchrow(self, sql: str, *args):
                    if "FROM reports" in sql:
                        return refs_row
                    return await app_conn.fetchrow(sql, *args)

                async def fetchval(self, sql: str, *args):
                    return await app_conn.fetchval(sql, *args)

            source = await clips.resolve_audio_source(_Conn(), report_id=report_id)
            assert source.kind == "session" and source.retained_from_ms == 0

            raw = await audio_store.get(
                key=source.object_key, tenant_id=TENANT_A, aad=source.aad
            )
            assert raw == wav  # whole-object GCM decrypt round-trip

            # Patient's turn: 2000–4500 ms (+300 pad → 1700–4800).
            got = audio_slicer.slice_pcm(
                audio_slicer.wav_to_pcm(raw), start_ms=2000, end_ms=4500, pad_ms=300
            )
            reference = pcm[1700 * 32 : 4800 * 32]
            assert hashlib.sha256(got).hexdigest() == hashlib.sha256(reference).hexdigest()

            # ── diarized listing: the patient turn carries the speaker ──
            transcript_back = await clips.load_session_transcript(
                _Conn(), session_id=session_id
            )
            segs = clips.segments_from_transcript(
                transcript_back, segment_ids=[seg_patient]
            )
            assert len(segs) == 1
            assert segs[0].speaker_role == "patient"
            assert (segs[0].start_ms, segs[0].end_ms) == (2000, 4500)

            # ── encode → encrypted clip object → token → stream back ──
            opus = await audio_slicer.encode_opus(got)
            assert opus.startswith(b"OggS")
            clip_id = uuid4()
            clip_key = f"clips/{TENANT_A}/{clip_id}.ogg.enc"
            await clips_store.put(
                key=clip_key, plaintext=opus, tenant_id=TENANT_A, aad=clip_id.bytes
            )
            token, _ = clips.mint_clip_token(
                "aa" * 32, tenant_id=TENANT_A, clip_id=clip_id, ttl_seconds=300
            )
            assert clips.verify_clip_token(
                "aa" * 32, tenant_id=TENANT_A, clip_id=clip_id, token=token
            )
            streamed = await clips_store.get(
                key=clip_key, tenant_id=TENANT_A, aad=clip_id.bytes
            )
            assert streamed == opus
            await clips_store.delete(key=clip_key)
        finally:
            await app_conn.close()

        # ── erased audio → the honest 410 signal ────────────────────
        from storage import ObjectNotFoundError

        await audio_store.delete(key=object_key)
        with pytest.raises(ObjectNotFoundError):
            await audio_store.get(key=object_key, tenant_id=TENANT_A, aad=session_id.bytes)
    finally:
        await su.execute("DELETE FROM dictation_sessions WHERE id=$1", session_id)
        await su.execute("DELETE FROM audio_files WHERE id=$1", audio_id)
        await su.close()
        await crypto_pool.close()
