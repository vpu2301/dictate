"""Encode/decode the wire formats.

- Text frames: JSON ClientMessage / ServerMessage discriminated unions.
- Binary frames: ``[4-byte BE seq][opaque Opus payload]``.

Failures raise :class:`BadMessageError` with an :class:`ErrorCode`. The
upgrade handler converts that into either an error message or a close.
"""

from __future__ import annotations

import json
import struct
from typing import Final, cast

from pydantic import TypeAdapter, ValidationError

from .error_catalogue import ErrorCode
from .messages import (
    PROTOCOL_VERSION_V1,
    PROTOCOL_VERSION_V2,
    AudioFrame,
    ClientMessage,
    ClientMessageV2,
    FinalV2,
    Partial,
    PartialV2,
    ServerMessage,
    ServerMessageV2,
    SessionStarted,
    SessionStartedV2,
    SpeakerMappingUpdated,
)
from .messages import (
    Final as FinalMessage,
)

SUBPROTOCOL: Final = "medical-dictation.v1"
SUBPROTOCOL_V2: Final = "medical-dictation.v2"
PROTOCOL_VERSION: Final = PROTOCOL_VERSION_V1

# Subprotocol name <-> wire version. The client offers a list in
# Sec-WebSocket-Protocol; the server selects per SUBPROTOCOL_PREFERENCE
# (a v2-capable client that offers both gets v2). One session is exactly
# one version for its whole lifetime.
VERSION_BY_SUBPROTOCOL: Final = {
    SUBPROTOCOL: PROTOCOL_VERSION_V1,
    SUBPROTOCOL_V2: PROTOCOL_VERSION_V2,
}
SUBPROTOCOL_PREFERENCE: Final = (SUBPROTOCOL_V2, SUBPROTOCOL)

# Server->client classes that exist only on the v2 wire. encode_server
# refuses to serialise them for a v1 session — a programmer mistake must
# fail loudly server-side, not surface as an extra="forbid" rejection in
# a v1 client.
_V2_ONLY_SERVER_TYPES: Final = (SessionStartedV2, PartialV2, FinalV2, SpeakerMappingUpdated)

# The mirror guard: these three have v2 counterparts, so emitting the v1
# class on a v2 session would silently drop the speaker fields the
# client is waiting for. Compared by EXACT type — the V2 classes are
# subclasses, so isinstance() would match them too.
_V1_ONLY_SERVER_TYPES: Final = (SessionStarted, Partial, FinalMessage)

# Hard limits — also enforced at the WS framing layer; this is defence
# in depth so a bug elsewhere can't slip a giant frame past us.
MAX_BINARY_FRAME_BYTES: Final = 8 * 1024
MIN_BINARY_FRAME_BYTES: Final = 5  # 4 bytes seq + at least 1 byte Opus
SEQ_HEADER_BYTES: Final = 4


class BadMessageError(Exception):
    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail


_client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
_server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
_client_adapter_v2: TypeAdapter[ClientMessageV2] = TypeAdapter(ClientMessageV2)
_server_adapter_v2: TypeAdapter[ServerMessageV2] = TypeAdapter(ServerMessageV2)


def decode_text(
    frame: str, protocol_version: int = PROTOCOL_VERSION_V1
) -> ClientMessage | ClientMessageV2:
    """Parse a text frame into a strict ClientMessage for the session's
    negotiated protocol version.

    Raises BadMessageError on any malformed JSON, unknown ``type``,
    unexpected field, missing required field, wrong type, or a
    ``protocol_version`` that does not match the negotiated one (a v2
    field reaching a v1 session dies here or on extra="forbid").
    """
    try:
        raw = json.loads(frame)
    except json.JSONDecodeError as exc:
        raise BadMessageError(
            ErrorCode.BAD_MESSAGE,
            f"text frame is not valid JSON: {exc.msg}",
        ) from exc

    if not isinstance(raw, dict):
        raise BadMessageError(ErrorCode.BAD_MESSAGE, "text frame must be a JSON object")

    # Defensive protocol_version check before the union dispatch — gives
    # a clearer error than the discriminator's "unknown type". The wire
    # version is pinned by subprotocol negotiation; a mismatching field
    # is a protocol violation, not a renegotiation.
    ver = raw.get("protocol_version", protocol_version)
    if not isinstance(ver, int) or ver != protocol_version:
        raise BadMessageError(
            ErrorCode.UNSUPPORTED_PROTOCOL,
            f"protocol_version={ver!r} not supported (session negotiated v{protocol_version})",
        )

    adapter = _client_adapter_v2 if protocol_version == PROTOCOL_VERSION_V2 else _client_adapter
    try:
        return adapter.validate_python(raw)
    except ValidationError as exc:
        # Surface the FIRST error to the client; full detail in server logs.
        first = exc.errors()[0] if exc.errors() else {"msg": "invalid"}
        loc = ".".join(str(p) for p in first.get("loc", []))
        raise BadMessageError(
            ErrorCode.BAD_MESSAGE,
            f"validation: {loc}: {first.get('msg', 'invalid')}",
        ) from exc


def decode_binary(frame: bytes) -> AudioFrame:
    """Parse a binary frame as ``[4-byte BE seq][Opus bytes]``."""
    if len(frame) < MIN_BINARY_FRAME_BYTES:
        raise BadMessageError(
            ErrorCode.BAD_MESSAGE,
            f"binary frame is {len(frame)} bytes; minimum {MIN_BINARY_FRAME_BYTES}",
        )
    if len(frame) > MAX_BINARY_FRAME_BYTES:
        raise BadMessageError(
            ErrorCode.BAD_MESSAGE,
            f"binary frame is {len(frame)} bytes; maximum {MAX_BINARY_FRAME_BYTES}",
        )
    (seq,) = struct.unpack(">I", frame[:SEQ_HEADER_BYTES])
    opus = frame[SEQ_HEADER_BYTES:]
    return AudioFrame(seq=seq, opus=opus)


def encode_server(
    message: ServerMessage | ServerMessageV2,
    protocol_version: int = PROTOCOL_VERSION_V1,
) -> str:
    """Serialise a server message to canonical JSON for the session's
    negotiated protocol version.

    Uses Pydantic's `.model_dump_json()` so the schema validation also
    enforces the wire shape — a programmer mistake (wrong type, missing
    field) raises here instead of being silently truncated at the peer.
    A v2-only message on a v1 session raises: v1 output must stay
    byte-stable, and pydantic would otherwise silently serialise the
    subclass through its v1 parent schema, dropping the speaker fields.
    """
    if protocol_version == PROTOCOL_VERSION_V2:
        if type(message) in _V1_ONLY_SERVER_TYPES:
            raise BadMessageError(
                ErrorCode.INTERNAL,
                f"{type(message).__name__} has a v2 counterpart; session negotiated v2",
            )
        return _server_adapter_v2.dump_json(cast(ServerMessageV2, message)).decode("utf-8")
    if isinstance(message, _V2_ONLY_SERVER_TYPES):
        raise BadMessageError(
            ErrorCode.INTERNAL,
            f"{type(message).__name__} is v2-only; session negotiated v1",
        )
    # `mode="json"` ensures bytes/datetimes/UUIDs round-trip as strings.
    return _server_adapter.dump_json(message).decode("utf-8")


def subprotocol_for_version(protocol_version: int) -> str:
    return SUBPROTOCOL_V2 if protocol_version == PROTOCOL_VERSION_V2 else SUBPROTOCOL


def negotiate_subprotocol(offered: list[str]) -> str | None:
    """Server-side selection from the client's offered list, in
    SUBPROTOCOL_PREFERENCE order. None -> no supported subprotocol."""
    for candidate in SUBPROTOCOL_PREFERENCE:
        if candidate in offered:
            return candidate
    return None
