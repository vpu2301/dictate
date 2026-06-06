"""Encode/decode the wire formats.

- Text frames: JSON ClientMessage / ServerMessage discriminated unions.
- Binary frames: ``[4-byte BE seq][opaque Opus payload]``.

Failures raise :class:`BadMessageError` with an :class:`ErrorCode`. The
upgrade handler converts that into either an error message or a close.
"""

from __future__ import annotations

import json
import struct
from typing import Final

from pydantic import TypeAdapter, ValidationError

from .error_catalogue import ErrorCode
from .messages import AudioFrame, ClientMessage, PROTOCOL_VERSION_V1, ServerMessage

SUBPROTOCOL: Final = "medical-dictation.v1"
PROTOCOL_VERSION: Final = PROTOCOL_VERSION_V1

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


def decode_text(frame: str) -> ClientMessage:
    """Parse a text frame into a strict ClientMessage.

    Raises BadMessageError on any malformed JSON, unknown ``type``,
    unexpected field, missing required field, or wrong type.
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
    # a clearer error than the discriminator's "unknown type".
    ver = raw.get("protocol_version", PROTOCOL_VERSION)
    if not isinstance(ver, int) or ver != PROTOCOL_VERSION:
        raise BadMessageError(
            ErrorCode.UNSUPPORTED_PROTOCOL,
            f"protocol_version={ver!r} not supported (only v{PROTOCOL_VERSION})",
        )

    try:
        return _client_adapter.validate_python(raw)
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


def encode_server(message: ServerMessage) -> str:
    """Serialise a server message to canonical JSON.

    Uses Pydantic's `.model_dump_json()` so the schema validation also
    enforces the wire shape — a programmer mistake (wrong type, missing
    field) raises here instead of being silently truncated at the peer.
    """
    # `mode="json"` ensures bytes/datetimes/UUIDs round-trip as strings.
    return _server_adapter.dump_json(message).decode("utf-8")
