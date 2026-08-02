"""PCM slicing + opus encoding for audio-replay clips (sprint 15, ADR-0037).

The envelope is whole-object AES-GCM — there is no range decryption
(libs/crypto has no chunked mode), so the caller always holds the FULL
decrypted audio and slicing happens here, in memory:

    decrypt → normalize to s16le/16k/mono PCM → slice by ms (+pad) → opus

The PCM slice step is pure byte math (deterministic, checksummable —
the VERIFY contract); only decode of non-WAV containers and the final
opus encode shell out to ffmpeg (argument-array form, never a shell
string — the asr-worker ``audio_io`` doctrine).
"""

from __future__ import annotations

import asyncio
import io
import wave

SAMPLE_RATE_HZ = 16_000
BYTES_PER_SAMPLE = 2  # s16le
BYTES_PER_MS = SAMPLE_RATE_HZ * BYTES_PER_SAMPLE // 1000  # 32


class AudioClipError(Exception):
    pass


def wav_to_pcm(wav_bytes: bytes) -> bytes:
    """Fast path for the dictation-session WAV (PCM s16le, 16 kHz, mono,
    written by dictation-service finalize). Raises on any other layout —
    those go through ffmpeg instead."""
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            if (
                w.getnchannels() != 1
                or w.getframerate() != SAMPLE_RATE_HZ
                or w.getsampwidth() != BYTES_PER_SAMPLE
            ):
                raise AudioClipError(
                    f"unexpected WAV layout: ch={w.getnchannels()} "
                    f"rate={w.getframerate()} width={w.getsampwidth()}"
                )
            return w.readframes(w.getnframes())
    except wave.Error as exc:
        raise AudioClipError(f"not a readable WAV: {exc}") from exc


async def decode_to_pcm(
    audio_bytes: bytes, *, ffmpeg_path: str = "ffmpeg", timeout_seconds: float = 60.0
) -> bytes:
    """Any validated container (WAV/MP3/OGG/WebM/FLAC — the batch upload
    formats) → s16le 16 kHz mono PCM bytes, via ffmpeg subprocess."""
    args = [
        ffmpeg_path, "-loglevel", "error",
        "-i", "pipe:0",
        "-ac", "1", "-ar", str(SAMPLE_RATE_HZ),
        "-f", "s16le", "pipe:1",
    ]
    stdout = await _run_ffmpeg(args, audio_bytes, timeout_seconds=timeout_seconds)
    if not stdout:
        raise AudioClipError("ffmpeg produced zero PCM samples")
    return stdout


def slice_pcm(pcm: bytes, *, start_ms: int, end_ms: int, pad_ms: int = 0) -> bytes:
    """Pure, deterministic ms-addressed slice with symmetric pad, clamped
    to the buffer. Byte offsets are sample-aligned by construction
    (BYTES_PER_MS is a whole number of samples)."""
    if end_ms <= start_ms:
        raise AudioClipError("end_ms must be greater than start_ms")
    lo = max(0, (start_ms - pad_ms)) * BYTES_PER_MS
    hi = min(len(pcm), (end_ms + pad_ms) * BYTES_PER_MS)
    if lo >= len(pcm):
        raise AudioClipError("requested range lies beyond the retained audio")
    return pcm[lo:hi]


async def encode_opus(
    pcm: bytes, *, ffmpeg_path: str = "ffmpeg", timeout_seconds: float = 30.0
) -> bytes:
    """s16le/16k/mono PCM → Ogg/Opus at 24 kbps (speech-adequate, tiny)."""
    args = [
        ffmpeg_path, "-loglevel", "error",
        "-f", "s16le", "-ar", str(SAMPLE_RATE_HZ), "-ac", "1",
        "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "24k",
        "-f", "ogg", "pipe:1",
    ]
    stdout = await _run_ffmpeg(args, pcm, timeout_seconds=timeout_seconds)
    if not stdout:
        raise AudioClipError("ffmpeg produced an empty opus stream")
    return stdout


async def _run_ffmpeg(args: list[str], stdin: bytes, *, timeout_seconds: float) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin), timeout=timeout_seconds
        )
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise AudioClipError("ffmpeg timed out") from exc
    if proc.returncode != 0:
        raise AudioClipError(
            f"ffmpeg failed (rc={proc.returncode}): "
            f"{stderr.decode('utf-8', 'replace')[:512]}"
        )
    return stdout
