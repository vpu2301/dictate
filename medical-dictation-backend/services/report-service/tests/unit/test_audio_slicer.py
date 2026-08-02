"""PCM slicing — deterministic byte math (the checksum VERIFY contract).

ffmpeg-dependent paths (container decode, opus encode) run only when an
ffmpeg binary is present; the pure slice math always runs.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import struct
import wave

import pytest

from report_service.domain import audio_slicer

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _make_wav(duration_ms: int, *, rate: int = 16_000) -> tuple[bytes, bytes]:
    """Synthetic s16le/mono WAV whose PCM is a deterministic sample counter —
    every 2-byte frame is unique, so slice boundaries are byte-provable."""
    n = rate * duration_ms // 1000
    pcm = b"".join(struct.pack("<h", i % 32768) for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue(), pcm


def test_wav_to_pcm_roundtrip():
    wav, pcm = _make_wav(1000)
    assert audio_slicer.wav_to_pcm(wav) == pcm


def test_wav_wrong_layout_rejected():
    wav, _ = _make_wav(100, rate=44_100)
    with pytest.raises(audio_slicer.AudioClipError, match="unexpected WAV layout"):
        audio_slicer.wav_to_pcm(wav)
    with pytest.raises(audio_slicer.AudioClipError, match="not a readable WAV"):
        audio_slicer.wav_to_pcm(b"definitely not a wav")


def test_slice_exact_window_checksum():
    """The slice IS the reference slice: [start-pad, end+pad] ms × 32 B/ms."""
    _, pcm = _make_wav(10_000)
    got = audio_slicer.slice_pcm(pcm, start_ms=2_000, end_ms=3_000, pad_ms=300)
    reference = pcm[1_700 * 32 : 3_300 * 32]
    assert got == reference
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(reference).hexdigest()
    assert len(got) == 1_600 * 32  # 1000 ms span + 2×300 ms pad


def test_slice_clamps_at_edges():
    _, pcm = _make_wav(1_000)
    got = audio_slicer.slice_pcm(pcm, start_ms=0, end_ms=900, pad_ms=300)
    assert got == pcm[: 1_000 * 32]  # lead pad clamps to 0, tail pad to EOF


def test_slice_beyond_retained_audio_raises():
    _, pcm = _make_wav(1_000)
    with pytest.raises(audio_slicer.AudioClipError, match="beyond the retained audio"):
        audio_slicer.slice_pcm(pcm, start_ms=5_000, end_ms=6_000)
    with pytest.raises(audio_slicer.AudioClipError, match="greater than start_ms"):
        audio_slicer.slice_pcm(pcm, start_ms=500, end_ms=500)


@needs_ffmpeg
async def test_opus_encode_produces_ogg():
    _, pcm = _make_wav(1_000)
    ogg = await audio_slicer.encode_opus(pcm)
    assert ogg.startswith(b"OggS")


@needs_ffmpeg
async def test_container_decode_roundtrip():
    """WAV through the ffmpeg container path decodes to the same sample
    count the fast path yields (batch uploads: MP3/OGG/WebM/FLAC)."""
    wav, pcm = _make_wav(1_000)
    decoded = await audio_slicer.decode_to_pcm(wav)
    assert len(decoded) == len(pcm)
