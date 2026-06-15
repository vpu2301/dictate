"""Step 6 — codec + sample rate + channels in allow-list."""

from __future__ import annotations

from typing import Final

from .duration import ProbeOutput
from .result import ValidationCode, ValidationResult, ok, reject

ALLOWED_CODECS: Final[frozenset[str]] = frozenset(
    {"pcm_s16le", "pcm_s24le", "mp3", "vorbis", "opus", "flac"}
)


def validate_codec(
    probe: ProbeOutput,
    *,
    min_sample_rate_hz: int,
    max_channels: int,
) -> ValidationResult:
    if probe.codec not in ALLOWED_CODECS:
        return reject(
            ValidationCode.CODEC_NOT_ALLOWED,
            f"codec {probe.codec!r} is not in the allow-list ({sorted(ALLOWED_CODECS)})",
        )
    if probe.sample_rate_hz < min_sample_rate_hz:
        return reject(
            ValidationCode.SAMPLE_RATE_TOO_LOW,
            f"sample rate {probe.sample_rate_hz} Hz is below the minimum {min_sample_rate_hz} Hz",
        )
    if probe.channels > max_channels:
        return reject(
            ValidationCode.CHANNELS_EXCEEDED,
            f"audio has {probe.channels} channels; max is {max_channels}",
        )
    return ok()
