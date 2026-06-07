"""Wire-protocol codec tests.

Covers every error-path the spec lists in §2 verification:
- malformed JSON → BadMessageError(bad_message)
- extra field → BadMessageError(bad_message)
- wrong type → BadMessageError(bad_message)
- subprotocol enforcement is in upgrade.py, not here
- binary frame size limits
- audio_frame parsing
"""

from __future__ import annotations

import struct
from uuid import uuid4

import pytest

from dictation_service.protocol import (
    AudioFrame,
    BadMessageError,
    EndSession,
    ErrorCode,
    Pause,
    RetransmitRange,
    StartSession,
    decode_binary,
    decode_text,
    encode_server,
)
from dictation_service.protocol.codec import (
    MAX_BINARY_FRAME_BYTES,
    MIN_BINARY_FRAME_BYTES,
)
from dictation_service.protocol.messages import (
    Final,
    Heartbeat,
    Partial,
    SessionStarted,
)


def test_decode_text_start_session_minimal() -> None:
    prompt = str(uuid4())
    msg = decode_text(f'{{"type":"start_session","prompt_id":"{prompt}","language":"uk"}}')
    assert isinstance(msg, StartSession)
    assert msg.language == "uk"


def test_decode_text_end_session() -> None:
    msg = decode_text('{"type":"end_session"}')
    assert isinstance(msg, EndSession)


def test_decode_text_pause() -> None:
    msg = decode_text('{"type":"pause"}')
    assert isinstance(msg, Pause)


def test_decode_text_retransmit_range() -> None:
    msg = decode_text('{"type":"retransmit_range","from_seq":10,"to_seq":20}')
    assert isinstance(msg, RetransmitRange)
    assert msg.from_seq == 10 and msg.to_seq == 20


def test_decode_text_extra_field_rejected() -> None:
    """`extra="forbid"` defends against `is_admin`-style injection."""
    with pytest.raises(BadMessageError) as exc:
        decode_text('{"type":"end_session","is_admin":true}')
    assert exc.value.code == ErrorCode.BAD_MESSAGE


def test_decode_text_unknown_type_rejected() -> None:
    with pytest.raises(BadMessageError):
        decode_text('{"type":"unknown_message"}')


def test_decode_text_wrong_protocol_version_rejected() -> None:
    with pytest.raises(BadMessageError) as exc:
        decode_text(
            '{"type":"start_session","protocol_version":2,'
            '"prompt_id":"00000000-0000-0000-0000-000000000000","language":"uk"}'
        )
    assert exc.value.code == ErrorCode.UNSUPPORTED_PROTOCOL


def test_decode_text_invalid_json_rejected() -> None:
    with pytest.raises(BadMessageError) as exc:
        decode_text("{not json")
    assert exc.value.code == ErrorCode.BAD_MESSAGE


def test_decode_text_non_object_rejected() -> None:
    with pytest.raises(BadMessageError):
        decode_text('["not","an","object"]')


def test_decode_text_invalid_language_rejected() -> None:
    with pytest.raises(BadMessageError):
        decode_text(f'{{"type":"start_session","prompt_id":"{uuid4()}","language":"fr"}}')


def test_decode_binary_happy_path() -> None:
    payload = b"\x01\x02\x03"
    frame = struct.pack(">I", 42) + payload
    audio = decode_binary(frame)
    assert isinstance(audio, AudioFrame)
    assert audio.seq == 42
    assert audio.opus == payload


def test_decode_binary_too_small_rejected() -> None:
    with pytest.raises(BadMessageError) as exc:
        decode_binary(b"\x00" * (MIN_BINARY_FRAME_BYTES - 1))
    assert exc.value.code == ErrorCode.BAD_MESSAGE


def test_decode_binary_too_large_rejected() -> None:
    with pytest.raises(BadMessageError) as exc:
        decode_binary(b"\x00" * (MAX_BINARY_FRAME_BYTES + 1))
    assert exc.value.code == ErrorCode.BAD_MESSAGE


def test_encode_server_session_started_round_trips() -> None:
    sid = uuid4()
    msg = SessionStarted(
        session_id=sid,
        resumed=False,
        last_committed_seq=0,
        committed_audio_until_ms=0,
        server_time_ms=1000,
        model="large-v3",
        language="uk",
    )
    s = encode_server(msg)
    assert '"session_started"' in s
    assert str(sid) in s
    assert '"large-v3"' in s


def test_encode_server_heartbeat() -> None:
    s = encode_server(Heartbeat(server_time_ms=12345))
    assert '"heartbeat"' in s
    assert "12345" in s


def test_encode_server_final_voice_command_null() -> None:
    """Sprint-04 reservation: voice_command field present, always null."""
    sid = uuid4()
    s = encode_server(
        Final(
            session_id=sid,
            seq=1,
            text="hello",
            start_ms=0,
            end_ms=500,
            words=[],
            avg_confidence=0.9,
        )
    )
    assert '"voice_command":null' in s
    assert '"is_provisional":false' in s


def test_encode_server_partial_has_no_voice_command() -> None:
    sid = uuid4()
    s = encode_server(
        Partial(
            session_id=sid,
            seq=1,
            text="hello",
            start_ms=0,
            end_ms=500,
            words=[],
            avg_confidence=0.9,
        )
    )
    assert '"voice_command"' not in s
