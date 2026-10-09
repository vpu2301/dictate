"""WAV validation for eval-recorder uploads.

The corpus format is fixed (eval/corpus/v1/README.md): 16 kHz, mono, PCM
16-bit. The SPA's encoder produces exactly that, but the format is an
invariant of the corpus, not a courtesy of one client — so the boundary
re-checks the actual bytes and refuses anything else. Refusal here is a 422
with a stable error code, never a stored-then-discovered-broken file.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

REQUIRED_SAMPLE_RATE = 16_000
REQUIRED_CHANNELS = 1
REQUIRED_BITS = 16


class WavFormatError(ValueError):
    """The upload is not a 16 kHz mono PCM16 WAV."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    duration_ms: int
    data_bytes: int


def parse_wav(data: bytes) -> WavInfo:
    """Parse and validate a RIFF/WAVE header; returns duration facts.

    Walks the chunk list rather than assuming fmt/data at fixed offsets —
    browser encoders are free to emit extra chunks (LIST, fact) and a valid
    file must not be refused for carrying one.
    """
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise WavFormatError("not_wav")

    fmt: tuple[int, int, int, int] | None = None  # format, channels, rate, bits
    data_size: int | None = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (chunk_size,) = struct.unpack_from("<I", data, pos + 4)
        body = pos + 8
        if chunk_id == b"fmt " and body + 16 <= len(data):
            audio_format, channels, rate = struct.unpack_from("<HHI", data, body)
            (bits,) = struct.unpack_from("<H", data, body + 14)
            fmt = (audio_format, channels, rate, bits)
        elif chunk_id == b"data":
            data_size = min(chunk_size, len(data) - body)
        # Chunks are word-aligned: an odd-sized chunk is followed by a pad byte.
        pos = body + chunk_size + (chunk_size % 2)

    if fmt is None or data_size is None:
        raise WavFormatError("not_wav")
    audio_format, channels, rate, bits = fmt
    if audio_format != 1 or channels != REQUIRED_CHANNELS or bits != REQUIRED_BITS:
        raise WavFormatError("wrong_format")
    if rate != REQUIRED_SAMPLE_RATE:
        raise WavFormatError("wrong_sample_rate")
    if data_size <= 0:
        raise WavFormatError("empty_audio")

    bytes_per_second = rate * channels * (bits // 8)
    duration_ms = round(data_size * 1000 / bytes_per_second)
    return WavInfo(sample_rate=rate, duration_ms=duration_ms, data_bytes=data_size)
