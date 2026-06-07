"""Stable error codes for the medical-dictation.v1 wire protocol.

These are PUBLIC. Once a frontend ships against them they're a contract:
new codes can be added, existing ones can't be renamed or repurposed.

`recoverable` indicates whether the client should attempt to recover
the session (reconnect, retransmit, restart) or treat the error as
terminal.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ErrorCode(StrEnum):
    # 4xx-ish (caller fault, non-recoverable in-session)
    BAD_MESSAGE = "bad_message"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    AUTH_INVALID = "auth_invalid"
    PAUSE_STATE_MISMATCH = "pause_state_mismatch"
    RETRANSMIT_TOO_LARGE = "retransmit_too_large"
    SESSION_NOT_FOUND = "session_not_found"
    RATE_LIMITED = "rate_limited"

    # 5xx-ish (server side)
    WORKER_FAILED = "worker_failed"
    AUDIO_DECODE_FAILED = "audio_decode_failed"
    GPU_FULL = "gpu_full"
    GAP_DETECTED = "gap_detected"
    HIGH_LATENCY = "high_latency"
    WORKER_OVERLOADED = "worker_overloaded"
    LOW_CONFIDENCE = "low_confidence"
    TOKEN_EXPIRED = "token_expired"
    INTERNAL = "internal"


# Codes flagged recoverable mean: client should not give up — retry,
# reconnect, or pause-then-resume can usually recover. Non-recoverable
# means the session is over; the client should surface a UI error.
RECOVERABLE: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.BAD_MESSAGE,
        ErrorCode.AUDIO_DECODE_FAILED,
        ErrorCode.GAP_DETECTED,
        ErrorCode.HIGH_LATENCY,
        ErrorCode.WORKER_OVERLOADED,
        ErrorCode.LOW_CONFIDENCE,
        ErrorCode.GPU_FULL,  # retry later
        ErrorCode.RATE_LIMITED,
        ErrorCode.RETRANSMIT_TOO_LARGE,
    }
)


def is_recoverable(code: ErrorCode | str) -> bool:
    try:
        return ErrorCode(code) in RECOVERABLE
    except ValueError:
        return False
