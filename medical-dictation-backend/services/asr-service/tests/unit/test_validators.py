"""Unit tests for the 8-step validation pipeline (steps 2–7)."""

from __future__ import annotations

import pytest

from asr_service.validators.codec import validate_codec
from asr_service.validators.duration import ProbeOutput, validate_duration
from asr_service.validators.hash import compute_hash
from asr_service.validators.magic_bytes import validate_magic_bytes
from asr_service.validators.mime import validate_mime
from asr_service.validators.size import validate_size


def test_mime_allow_list_accepts_wav() -> None:
    assert validate_mime("audio/wav").ok


def test_mime_allow_list_rejects_application_zip() -> None:
    r = validate_mime("application/zip")
    assert not r.ok
    assert r.code == "mime_not_allowed"


def test_magic_bytes_wav_happy_path() -> None:
    head = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 32
    assert validate_magic_bytes("audio/wav", head).ok


def test_magic_bytes_polyglot_rejected() -> None:
    # MP3 magic but declared as WAV.
    head = b"ID3" + b"\x00" * 32
    r = validate_magic_bytes("audio/wav", head)
    assert not r.ok
    assert r.code == "mime_mismatch"


def test_magic_bytes_short_input_rejected() -> None:
    r = validate_magic_bytes("audio/wav", b"RIFF")
    assert not r.ok


def test_magic_bytes_mp3_frame_sync_accepted() -> None:
    head = b"\xff\xfb" + b"\x00" * 32
    assert validate_magic_bytes("audio/mpeg", head).ok


def test_magic_bytes_ogg_accepted() -> None:
    head = b"OggS" + b"\x00" * 32
    assert validate_magic_bytes("audio/ogg", head).ok


def test_magic_bytes_webm_accepted() -> None:
    head = b"\x1aE\xdf\xa3" + b"\x00" * 32
    assert validate_magic_bytes("audio/webm", head).ok


def test_magic_bytes_flac_accepted() -> None:
    head = b"fLaC" + b"\x00" * 32
    assert validate_magic_bytes("audio/flac", head).ok


def test_size_at_cap_accepted() -> None:
    assert validate_size(100 * 1024 * 1024, max_mb=100).ok


def test_size_over_cap_rejected() -> None:
    r = validate_size(100 * 1024 * 1024 + 1, max_mb=100)
    assert not r.ok
    assert r.code == "size_exceeded"


def test_duration_unprobeable_rejected() -> None:
    r = validate_duration(None, max_seconds=1800)
    assert not r.ok
    assert r.code == "unprobeable"


def test_duration_over_cap_rejected() -> None:
    probe = ProbeOutput(
        duration_ms=31 * 60 * 1000, sample_rate_hz=16000, channels=1, codec="pcm_s16le"
    )
    r = validate_duration(probe, max_seconds=30 * 60)
    assert not r.ok
    assert r.code == "duration_exceeded"


def test_duration_accepted() -> None:
    probe = ProbeOutput(
        duration_ms=30 * 60 * 1000, sample_rate_hz=16000, channels=1, codec="pcm_s16le"
    )
    assert validate_duration(probe, max_seconds=30 * 60).ok


@pytest.mark.parametrize(
    "codec,sample_rate,channels,code",
    [
        ("ffv1", 16000, 1, "codec_not_allowed"),
        ("pcm_s16le", 4000, 1, "sample_rate_too_low"),
        ("pcm_s16le", 16000, 5, "channels_exceeded"),
    ],
)
def test_codec_rejections(
    codec: str, sample_rate: int, channels: int, code: str
) -> None:
    probe = ProbeOutput(
        duration_ms=60000, sample_rate_hz=sample_rate, channels=channels, codec=codec
    )
    r = validate_codec(probe, min_sample_rate_hz=8000, max_channels=2)
    assert not r.ok
    assert r.code == code


def test_codec_accepted() -> None:
    probe = ProbeOutput(
        duration_ms=60000, sample_rate_hz=16000, channels=1, codec="pcm_s16le"
    )
    assert validate_codec(probe, min_sample_rate_hz=8000, max_channels=2).ok


def test_hash_is_deterministic_and_correct_length() -> None:
    h1 = compute_hash(b"abc")
    h2 = compute_hash(b"abc")
    assert h1 == h2
    assert len(h1) == 32
