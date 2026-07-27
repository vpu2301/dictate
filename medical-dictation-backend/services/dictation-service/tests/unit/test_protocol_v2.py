"""medical-dictation.v2 protocol proofs (sprint 14).

Three contracts from the sprint-04 hand-off:
  1. v1 stays byte-stable — no v1 model changed, no v1 frame gains a key.
  2. A v1 client receiving a v2 message rejects it cleanly (extra="forbid").
  3. Negotiation selects correctly from the client's offered subprotocols.
"""

import json
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from dictation_service.protocol import codec
from dictation_service.protocol.error_catalogue import ErrorCode
from dictation_service.protocol.messages import (
    PROTOCOL_VERSION_V1,
    PROTOCOL_VERSION_V2,
    Final,
    FinalV2,
    Partial,
    PartialV2,
    ServerMessage,
    SessionStartedV2,
    SetSpeakerMapping,
    SpeakerMappingUpdated,
    StartSession,
    StartSessionV2,
)

SID = uuid4()


def _partial_v2() -> PartialV2:
    return PartialV2(
        session_id=SID,
        seq=3,
        text="пацієнт скаржиться",
        start_ms=0,
        end_ms=1200,
        avg_confidence=0.9,
        speaker="S2",
        speaker_confidence=0.81,
        speaker_mapping_hint={"S1": "doctor", "S2": "patient"},
    )


# ── 1. v1 byte-stability ──────────────────────────────────────────────


def test_v1_partial_wire_shape_has_no_speaker_keys() -> None:
    p = Partial(session_id=SID, seq=1, text="a", start_ms=0, end_ms=10, avg_confidence=1.0)
    s = codec.encode_server(p)
    assert '"speaker"' not in s
    assert '"speaker_confidence"' not in s
    assert '"speaker_mapping_hint"' not in s
    assert set(json.loads(s)) == {
        "type", "session_id", "seq", "text", "start_ms", "end_ms", "words", "avg_confidence",
    }


def test_v1_final_wire_shape_unchanged() -> None:
    f = Final(session_id=SID, seq=2, text="b", start_ms=0, end_ms=10, avg_confidence=1.0)
    s = codec.encode_server(f)
    assert set(json.loads(s)) == {
        "type", "session_id", "seq", "text", "start_ms", "end_ms", "words",
        "avg_confidence", "is_provisional", "voice_command",
    }


def test_v1_start_session_has_no_mode_field() -> None:
    assert "mode" not in StartSession.model_fields
    assert "speaker" not in Partial.model_fields
    assert "speaker" not in Final.model_fields


# ── 2. the extra="forbid" promise ─────────────────────────────────────


def test_v1_client_model_rejects_v2_partial() -> None:
    """A v2 Partial frame validated against the *v1* union — what a v1
    client does — must be rejected, not silently accepted."""
    v2_frame = json.loads(codec.encode_server(_partial_v2(), PROTOCOL_VERSION_V2))
    assert v2_frame["speaker"] == "S2"  # the frame really carries v2 fields
    v1_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
    with pytest.raises(ValidationError) as exc_info:
        v1_adapter.validate_python(v2_frame)
    assert any(e["type"] == "extra_forbidden" for e in exc_info.value.errors())


def test_v1_client_model_rejects_v2_final_and_mapping_update() -> None:
    v1_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
    f2 = FinalV2(
        session_id=SID, seq=1, text="x", start_ms=0, end_ms=5,
        avg_confidence=1.0, speaker="S1", speaker_confidence=0.9,
    )
    with pytest.raises(ValidationError):
        v1_adapter.validate_python(json.loads(codec.encode_server(f2, PROTOCOL_VERSION_V2)))
    smu = SpeakerMappingUpdated(
        session_id=SID, mapping={"S1": "doctor", "S2": "patient"}, confidence=0.9
    )
    with pytest.raises(ValidationError):
        # unknown discriminator value for a v1 client
        v1_adapter.validate_python(json.loads(codec.encode_server(smu, PROTOCOL_VERSION_V2)))


def test_v1_server_decode_rejects_v2_start_session_mode() -> None:
    frame = json.dumps(
        {
            "type": "start_session",
            "prompt_id": str(uuid4()),
            "language": "uk",
            "mode": "conversation",
        }
    )
    with pytest.raises(codec.BadMessageError) as exc_info:
        codec.decode_text(frame)  # v1 session
    assert exc_info.value.code is ErrorCode.BAD_MESSAGE


def test_v1_server_decode_rejects_set_speaker_mapping() -> None:
    frame = json.dumps({"type": "set_speaker_mapping", "mapping": {"S1": "doctor"}})
    with pytest.raises(codec.BadMessageError) as exc_info:
        codec.decode_text(frame)
    assert exc_info.value.code is ErrorCode.BAD_MESSAGE


def test_encode_server_refuses_v2_message_on_v1_session() -> None:
    with pytest.raises(codec.BadMessageError) as exc_info:
        codec.encode_server(_partial_v2(), PROTOCOL_VERSION_V1)
    assert exc_info.value.code is ErrorCode.INTERNAL


# ── 3. negotiation ────────────────────────────────────────────────────


def test_negotiation_v1_only_client() -> None:
    assert codec.negotiate_subprotocol(["medical-dictation.v1"]) == "medical-dictation.v1"


def test_negotiation_v2_only_client() -> None:
    assert codec.negotiate_subprotocol(["medical-dictation.v2"]) == "medical-dictation.v2"


def test_negotiation_prefers_v2_when_both_offered() -> None:
    assert (
        codec.negotiate_subprotocol(["medical-dictation.v1", "medical-dictation.v2"])
        == "medical-dictation.v2"
    )


def test_negotiation_rejects_unknown() -> None:
    assert codec.negotiate_subprotocol([]) is None
    assert codec.negotiate_subprotocol(["medical-dictation.v3", "chat"]) is None


def test_subprotocol_for_version_round_trip() -> None:
    assert codec.subprotocol_for_version(PROTOCOL_VERSION_V1) == "medical-dictation.v1"
    assert codec.subprotocol_for_version(PROTOCOL_VERSION_V2) == "medical-dictation.v2"


# ── v2 session decode/encode behaviour ────────────────────────────────


def test_v2_decode_start_session_conversation() -> None:
    frame = json.dumps(
        {
            "type": "start_session",
            "protocol_version": 2,
            "prompt_id": str(uuid4()),
            "language": "uk",
            "mode": "conversation",
            "encounter_id": str(uuid4()),
        }
    )
    msg = codec.decode_text(frame, PROTOCOL_VERSION_V2)
    assert isinstance(msg, StartSessionV2)
    assert msg.mode == "conversation"


def test_v2_decode_set_speaker_mapping() -> None:
    frame = json.dumps(
        {"type": "set_speaker_mapping", "mapping": {"S1": "patient", "S2": "doctor"}}
    )
    msg = codec.decode_text(frame, PROTOCOL_VERSION_V2)
    assert isinstance(msg, SetSpeakerMapping)
    assert msg.mapping == {"S1": "patient", "S2": "doctor"}


def test_v2_decode_rejects_bad_mapping_role() -> None:
    frame = json.dumps({"type": "set_speaker_mapping", "mapping": {"S1": "nurse"}})
    with pytest.raises(codec.BadMessageError):
        codec.decode_text(frame, PROTOCOL_VERSION_V2)


def test_version_gate_is_session_pinned_both_ways() -> None:
    v2_start = json.dumps(
        {"type": "start_session", "protocol_version": 2, "prompt_id": str(uuid4()), "language": "uk"}
    )
    with pytest.raises(codec.BadMessageError) as exc_info:
        codec.decode_text(v2_start, PROTOCOL_VERSION_V1)
    assert exc_info.value.code is ErrorCode.UNSUPPORTED_PROTOCOL

    v1_start = json.dumps(
        {"type": "start_session", "protocol_version": 1, "prompt_id": str(uuid4()), "language": "uk"}
    )
    with pytest.raises(codec.BadMessageError) as exc_info:
        codec.decode_text(v1_start, PROTOCOL_VERSION_V2)
    assert exc_info.value.code is ErrorCode.UNSUPPORTED_PROTOCOL


def test_v2_partial_encodes_speaker_fields_and_null_speaker() -> None:
    s = codec.encode_server(_partial_v2(), PROTOCOL_VERSION_V2)
    doc = json.loads(s)
    assert doc["speaker"] == "S2"
    assert doc["speaker_confidence"] == 0.81
    assert doc["speaker_mapping_hint"] == {"S1": "doctor", "S2": "patient"}
    # labels may trail: null speaker is legal on the v2 wire
    trailing = PartialV2(
        session_id=SID, seq=4, text="…", start_ms=1200, end_ms=1800, avg_confidence=0.8
    )
    doc2 = json.loads(codec.encode_server(trailing, PROTOCOL_VERSION_V2))
    assert doc2["speaker"] is None and doc2["speaker_confidence"] is None


def test_v2_session_started_carries_mode_and_version() -> None:
    started = SessionStartedV2(
        session_id=SID, server_time_ms=1, model="large-v3", language="uk", mode="conversation"
    )
    doc = json.loads(codec.encode_server(started, PROTOCOL_VERSION_V2))
    assert doc["protocol_version"] == 2
    assert doc["mode"] == "conversation"
