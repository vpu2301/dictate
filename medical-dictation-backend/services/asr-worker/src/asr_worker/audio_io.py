"""Audio decoding via ffmpeg subprocess.

Decodes any of the validated containers (WAV/MP3/OGG/WebM/FLAC) into
mono 16 kHz float32 PCM, which is what the VAD + Whisper expect.

We always invoke ffmpeg with the argument-array form so user-controlled
bytes never become shell metacharacters. The process is killed on
timeout and on cancellation.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np

logger = logging.getLogger(__name__)


class AudioDecodeError(Exception):
    pass


async def decode_to_pcm(
    audio_bytes: bytes,
    *,
    ffmpeg_path: str = "ffmpeg",
    sample_rate: int = 16_000,
    timeout_seconds: float = 30.0,
) -> np.ndarray:
    """Return mono 16 kHz float32 PCM as a 1-D numpy array."""
    args = [
        ffmpeg_path,
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=audio_bytes), timeout=timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise AudioDecodeError("ffmpeg decode timed out") from exc

    if proc.returncode != 0:
        raise AudioDecodeError(
            f"ffmpeg failed (rc={proc.returncode}): {stderr.decode('utf-8', 'replace')[:512]}"
        )

    if not stdout:
        raise AudioDecodeError("ffmpeg produced zero PCM samples")

    pcm = np.frombuffer(stdout, dtype=np.float32).copy()
    return pcm
