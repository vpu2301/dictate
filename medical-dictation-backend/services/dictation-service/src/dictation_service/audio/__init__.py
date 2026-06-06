"""Audio pipeline: Opus decoder + tmpfs ring buffer + gap policy.

Submodules are imported lazily so unit tests for one piece (e.g.,
``gap``) don't pay the numpy / cryptography cost of ``buffer``.
"""

from .gap import GapDecision, GapPolicy, GapResult, gap_decision

__all__ = [
    "GapDecision",
    "GapPolicy",
    "GapResult",
    "gap_decision",
    "OpusDecodeError",
    "OpusDecoder",
    "RingFull",
    "SessionAudioBuffer",
    "decode_pcm_view",
]


def __getattr__(name: str):
    if name in {"OpusDecodeError", "OpusDecoder"}:
        from .decoder import OpusDecodeError, OpusDecoder

        return {"OpusDecodeError": OpusDecodeError, "OpusDecoder": OpusDecoder}[name]
    if name in {"RingFull", "SessionAudioBuffer", "decode_pcm_view"}:
        from .buffer import RingFull, SessionAudioBuffer, decode_pcm_view

        return {
            "RingFull": RingFull,
            "SessionAudioBuffer": SessionAudioBuffer,
            "decode_pcm_view": decode_pcm_view,
        }[name]
    raise AttributeError(name)
