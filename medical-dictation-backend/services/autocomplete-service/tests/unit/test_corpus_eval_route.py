"""/corpus/eval — the WER eval recorder persistence surface, exercised with a
fake pool (test_corpus_route.py style); RLS is integration territory."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import zipfile
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from autocomplete_service.deps import install_state
from autocomplete_service.eval_script import ROW_BY_ID, SCRIPT, SCRIPT_VERSION
from autocomplete_service.eval_wav import WavFormatError, parse_wav
from autocomplete_service.routers.corpus_eval import (
    EvalScriptDTO,
    EvalTakeListDTO,
    SaveTakeRequest,
    delete_take,
    eval_script_view,
    export_takes,
    list_takes,
    save_take,
    take_audio,
)
from fastapi import HTTPException

from auth import Claims

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def _claims(roles=("clinician",)) -> Claims:
    return Claims(
        sub=uuid4(), tid=uuid4(), roles=list(roles), scope="openid",
        sid="s", iss="test", aud="mdx-api", exp=2_000_000_000, iat=1,
    )


def _wav(seconds: float = 1.0, *, rate: int = 16_000, channels: int = 1,
         bits: int = 16, audio_format: int = 1) -> bytes:
    """A minimal valid RIFF/WAVE of silence."""
    frames = int(rate * seconds)
    data = b"\x00" * (frames * channels * (bits // 8))
    fmt = struct.pack(
        "<HHIIHH", audio_format, channels, rate,
        rate * channels * (bits // 8), channels * (bits // 8), bits,
    )
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


def _b64(wav: bytes) -> str:
    return base64.b64encode(wav).decode()


class _FakeConn:
    def __init__(self, *, rows=None, row=None, execute_result="") -> None:
        self.rows = rows or []
        self.row = row
        self.execute_result = execute_result
        self.fetches: list[tuple[str, tuple]] = []
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        return self.execute_result

    async def fetch(self, sql: str, *args):
        self.fetches.append((sql, args))
        return self.rows

    async def fetchrow(self, sql: str, *args):
        self.fetches.append((sql, args))
        # Epic F gates every upload on a live speaker consent. These tests
        # are about the recorder, not about consent, so the fake speaker is
        # always consented — the refusal has its own test.
        if "corpus_speaker_consents" in sql:
            return {"consent": 1}
        return self.row

    async def fetchval(self, sql: str, *args):
        self.fetches.append((sql, args))
        return None

    def transaction(self):
        class _Tx:
            async def start(self): ...
            async def commit(self): ...
            async def rollback(self): ...
        return _Tx()


class _Acquire:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakePool:
    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def write_event(self, **kw):
        self.events.append(kw)


class _FakeRateLimiter:
    def __init__(self, allowed=True) -> None:
        self.allowed = allowed

    async def check(self, *, user_id):
        return (self.allowed, 0 if self.allowed else 30)


class _State:
    def __init__(self, conn) -> None:
        self.app_pool = _FakePool(conn)
        self.audit_writer = _FakeAudit()
        self.phrase_rate_limiter = _FakeRateLimiter()


def _take_row(script_id="uk-cardiology-101", **over) -> dict:
    wav = _wav(1.5)
    row = {
        "id": uuid4(),
        "script_id": script_id,
        "script_version": SCRIPT_VERSION,
        "recorded_by": uuid4(),
        "language": "uk",
        "specialty": "cardiology",
        "subset": None,
        "condition": "headset",
        "condition_confirmed": True,
        "duration_ms": 1500,
        "audio_sha256": hashlib.sha256(wav).hexdigest(),
        "size_bytes": len(wav),
        "flagged_bad": False,
        "flagged_note": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(over)
    return row


# ── the WAV gate ────────────────────────────────────────────────────────


def test_parse_wav_accepts_the_corpus_format():
    info = parse_wav(_wav(2.0))
    assert info.sample_rate == 16_000
    assert info.duration_ms == 2000


@pytest.mark.parametrize(
    ("wav", "code"),
    [
        (b"not audio at all, just bytes that are long enough to pass length" * 2, "not_wav"),
        (_wav(1.0, rate=8_000), "wrong_sample_rate"),
        (_wav(1.0, channels=2), "wrong_format"),
        (_wav(1.0, audio_format=3), "wrong_format"),
    ],
    ids=["not-wav", "8khz", "stereo", "float-pcm"],
)
def test_parse_wav_refuses_other_formats(wav, code):
    with pytest.raises(WavFormatError) as exc:
        parse_wav(wav)
    assert exc.value.code == code


# ── script ──────────────────────────────────────────────────────────────


async def test_script_serves_every_row_with_gold_text():
    install_state(_State(_FakeConn()))
    out = await eval_script_view(_claims())
    assert isinstance(out, EvalScriptDTO)
    assert out.version == SCRIPT_VERSION
    assert len(out.items) == len(SCRIPT)
    by_id = {i.id: i for i in out.items}
    # Since Epic B the gold IS the spoken form for every vendored line — the
    # style guide's rule stated as an assertion about what the recorder is
    # told to read against.
    for item in out.items:
        assert item.transcript == item.say, item.id
    assert by_id["uk-numbers-001"].transcript.startswith(
        "Артеріальний тиск сто сорок на дев'яносто"
    )
    # suggested condition rides along
    assert by_id["uk-noisy-001"].condition == "phone-speaker-distance"


# ── save ────────────────────────────────────────────────────────────────


async def test_save_take_stores_server_text_and_audits():
    wav = _wav(1.5)
    conn = _FakeConn(row=_take_row())
    state = _State(conn)
    install_state(state)
    claims = _claims()

    out = await save_take(
        "uk-cardiology-101",
        SaveTakeRequest(condition_confirmed=True, condition="headset", audio_wav_base64=_b64(wav),
                        say=ROW_BY_ID["uk-cardiology-101"]["say"]),
        claims,
    )

    assert out.script_id == "uk-cardiology-101"
    # fetches[0] is the Epic F consent probe; the upsert is next.
    sql, args = conn.fetches[1]
    assert "INSERT INTO corpus_eval_takes" in sql
    assert args[0] == claims.tid and args[3] == claims.sub
    # the stored text is the SERVER's script line, not anything client-sent
    assert args[7] == ROW_BY_ID["uk-cardiology-101"]["say"]
    # duration measured from the bytes, sha computed server-side
    assert args[10] == 1500
    assert args[12] == hashlib.sha256(wav).hexdigest()
    assert args[13] == wav

    event = state.audit_writer.events[0]
    assert event["kind"] == "corpus.eval_take_saved"
    assert event["payload"]["script_id"] == "uk-cardiology-101"
    assert "say" not in event["payload"]


async def test_save_take_unknown_script_404():
    install_state(_State(_FakeConn()))
    with pytest.raises(HTTPException) as exc:
        await save_take(
            "uk-nonexistent-999",
            SaveTakeRequest(condition_confirmed=True, condition="headset", audio_wav_base64=_b64(_wav())),
            _claims(),
        )
    assert exc.value.status_code == 404


async def test_save_take_script_drift_409():
    conn = _FakeConn()
    install_state(_State(conn))
    with pytest.raises(HTTPException) as exc:
        await save_take(
            "uk-cardiology-101",
            SaveTakeRequest(condition_confirmed=True, condition="headset", audio_wav_base64=_b64(_wav()),
                            say="зовсім інший текст, який показав застарілий клієнт"),
            _claims(),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == {"error": "script_drift"}
    # Nothing was WRITTEN — the only query is the consent probe that runs
    # before the audio is even decoded.
    assert [s for s, _ in conn.fetches if "INSERT" in s or "UPDATE" in s] == []  # refused before any SQL


@pytest.mark.parametrize(
    ("b64", "code"),
    [
        ("!!!! not base64 !!!!" + "a" * 60, "bad_base64"),
        (_b64(b"x" * 4096), "not_wav"),
        (_b64(_wav(1.0, rate=44_100)), "wrong_sample_rate"),
        (_b64(_wav(0.1)), "bad_duration"),  # < 300 ms
    ],
    ids=["bad-base64", "not-wav", "44khz", "too-short"],
)
async def test_save_take_refuses_bad_audio(b64, code):
    conn = _FakeConn()
    install_state(_State(conn))
    with pytest.raises(HTTPException) as exc:
        await save_take(
            "uk-cardiology-101",
            SaveTakeRequest(condition_confirmed=True, condition="headset", audio_wav_base64=b64),
            _claims(),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == code
    # Nothing was WRITTEN — the only query is the consent probe that runs
    # before the audio is even decoded.
    assert [s for s, _ in conn.fetches if "INSERT" in s or "UPDATE" in s] == []


async def test_save_take_rate_limited_429():
    state = _State(_FakeConn())
    state.phrase_rate_limiter = _FakeRateLimiter(allowed=False)
    install_state(state)
    with pytest.raises(HTTPException) as exc:
        await save_take(
            "uk-cardiology-101",
            SaveTakeRequest(condition_confirmed=True, condition="headset", audio_wav_base64=_b64(_wav())),
            _claims(),
        )
    assert exc.value.status_code == 429


# ── list / audio / delete ───────────────────────────────────────────────


async def test_list_takes_maps_rows_and_sums_duration():
    rows = [_take_row(), _take_row(script_id="uk-numbers-001", duration_ms=2500,
                                   subset="numbers_doses_units")]
    install_state(_State(_FakeConn(rows=rows)))
    out = await list_takes(_claims())
    assert isinstance(out, EvalTakeListDTO)
    assert [t.script_id for t in out.items] == ["uk-cardiology-101", "uk-numbers-001"]
    assert out.total_duration_ms == 4000
    assert out.items[1].subset == "numbers_doses_units"


async def test_take_audio_serves_stored_bytes():
    wav = _wav(1.0)
    sha = hashlib.sha256(wav).hexdigest()
    install_state(
        _State(
            _FakeConn(row={"audio_wav": wav, "audio_sha256": sha, "condition": "headset"})
        )
    )
    resp = await take_audio("uk-cardiology-101", _claims())
    assert resp.media_type == "audio/wav"
    assert resp.body == wav
    assert resp.headers["x-audio-sha256"] == sha


async def test_take_audio_404_when_missing():
    install_state(_State(_FakeConn(row=None)))
    with pytest.raises(HTTPException) as exc:
        await take_audio("uk-cardiology-101", _claims())
    assert exc.value.status_code == 404


async def test_delete_take_removes_and_audits():
    conn = _FakeConn(execute_result="DELETE 1")
    state = _State(conn)
    install_state(state)
    resp = await delete_take("uk-cardiology-101", _claims())
    assert resp.status_code == 204
    assert state.audit_writer.events[0]["kind"] == "corpus.eval_take_deleted"


async def test_delete_take_404_when_missing():
    state = _State(_FakeConn(execute_result="DELETE 0"))
    install_state(state)
    with pytest.raises(HTTPException) as exc:
        await delete_take("uk-cardiology-101", _claims())
    assert exc.value.status_code == 404
    assert state.audit_writer.events == []


# ── export ──────────────────────────────────────────────────────────────


async def test_export_builds_corpus_layout_with_verifiable_digests():
    wav = _wav(1.5)
    rows = [
        {
            "script_id": "uk-cardiology-101", "script_version": SCRIPT_VERSION,
            "language": "uk", "specialty": "cardiology", "subset": None,
            "say": ROW_BY_ID["uk-cardiology-101"]["say"],
            "transcript": ROW_BY_ID["uk-cardiology-101"]["say"],
            "condition": "headset", "duration_ms": 1500,
            "audio_sha256": hashlib.sha256(wav).hexdigest(), "audio_wav": wav,
            "source": "builtin", "paired": False,
        },
        {
            "script_id": "uk-noisy-001", "script_version": SCRIPT_VERSION,
            "language": "uk", "specialty": "general", "subset": "phone_mic_noisy",
            "say": ROW_BY_ID["uk-noisy-001"]["say"],
            "transcript": ROW_BY_ID["uk-noisy-001"]["say"],
            "condition": "phone-speaker-distance", "duration_ms": 1500,
            "audio_sha256": hashlib.sha256(wav).hexdigest(), "audio_wav": wav,
            "source": "builtin", "paired": False,
        },
    ]
    state = _State(_FakeConn(rows=rows))
    install_state(state)

    resp = await export_takes(_claims())
    assert resp.media_type == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(resp.body))
    names = set(zf.namelist())
    assert "uk-cardiology-101/audio.wav" in names
    assert "subsets/phone_mic_noisy/uk-noisy-001/audio.wav" in names
    assert "manifest-fragment.json" in names and "README-UNPACK.txt" in names

    fragment = json.loads(zf.read("manifest-fragment.json"))
    assert fragment["schema_version"] == 1
    by_id = {u["utterance_id"]: u for u in fragment["utterances"]}
    # the digests describe exactly the bytes in the archive
    entry = by_id["uk-noisy-001"]
    assert entry["subset"] == "phone_mic_noisy"
    assert entry["sha256"]["audio.wav"] == hashlib.sha256(wav).hexdigest()
    transcript = zf.read("subsets/phone_mic_noisy/uk-noisy-001/transcript.txt")
    assert entry["sha256"]["transcript.txt"] == hashlib.sha256(transcript).hexdigest()
    metadata = json.loads(zf.read("subsets/phone_mic_noisy/uk-noisy-001/metadata.json"))
    assert metadata["condition"] == "phone-speaker-distance"
    assert metadata["dictation_source"] == "authored_by_clinician"

    assert state.audit_writer.events[0]["kind"] == "corpus.eval_exported"
    assert state.audit_writer.events[0]["payload"] == {
        "utterances": 2,
        "snapshot_version": None,
    }


async def test_export_404_when_empty():
    state = _State(_FakeConn(rows=[]))
    install_state(state)
    with pytest.raises(HTTPException) as exc:
        await export_takes(_claims())
    assert exc.value.status_code == 404
    assert state.audit_writer.events == []
