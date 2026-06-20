"""Audio pipeline: Opus decoder + tmpfs ring buffer + gap policy.

Submodules are imported lazily so unit tests for one piece (e.g.,
``gap``) don't pay the numpy / cryptography cost of ``buffer``.
"""

from typing import TYPE_CHECKING, Any

from .gap import GapDecision, GapPolicy, GapResult, gap_decision

if TYPE_CHECKING:
    from .buffer import RingFullError, SessionAudioBuffer, decode_pcm_view
    from .decoder import OpusDecodeError, OpusDecoder

__all__ = [
    "GapDecision",
    "GapPolicy",
    "GapResult",
    "gap_decision",
    "OpusDecodeError",
    "OpusDecoder",
    "RingFullError",
    "SessionAudioBuffer",
    "decode_pcm_view",
]


def __getattr__(name: str) -> Any:
    if name in {"OpusDecodeError", "OpusDecoder"}:
        from .decoder import OpusDecodeError, OpusDecoder

        return {"OpusDecodeError": OpusDecodeError, "OpusDecoder": OpusDecoder}[name]
    if name in {"RingFullError", "SessionAudioBuffer", "decode_pcm_view"}:
        from .buffer import RingFullError, SessionAudioBuffer, decode_pcm_view

        return {
            "RingFullError": RingFullError,
            "SessionAudioBuffer": SessionAudioBuffer,
            "decode_pcm_view": decode_pcm_view,
        }[name]
    raise AttributeError(name)
