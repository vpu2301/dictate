"""Step 5 — probe duration with ffprobe; reject > MD_ASR_MAX_DURATION_SECONDS.

ffprobe is invoked with the strict ``-of json`` output and timeout. The
subprocess is started with the argument-array form (never a shell
string) so user-controlled bytes can never inject shell metacharacters.

The probe also returns the codec, sample rate, and channel count, which
the next validator (codec) consumes — kept in :class:`ProbeOutput` to
avoid running ffprobe twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from .result import ValidationCode, ValidationResult, ok, reject

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProbeOutput:
    duration_ms: int
    sample_rate_hz: int
    channels: int
    codec: str


async def probe_audio(
    path: str,
    *,
    ffprobe_path: str = "ffprobe",
    timeout_seconds: float = 5.0,
) -> ProbeOutput | None:
    """Run ffprobe on the file and return parsed metadata.

    Returns ``None`` if the file is unprobeable (corrupt headers, codec
    we don't recognise, or ffprobe times out / crashes).
    """
    args = [
        ffprobe_path,
        "-v",
        "error",
        "-show_streams",
        "-select_streams",
        "a:0",
        "-show_format",
        "-of",
        "json",
        path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.info("ffprobe.timeout", extra={"path": path})
            return None
    except FileNotFoundError:
        logger.error("ffprobe.not_found", extra={"ffprobe_path": ffprobe_path})
        return None

    if proc.returncode != 0:
        return None

    try:
        doc = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    fmt = doc.get("format", {})
    streams = doc.get("streams") or []
    if not streams:
        return None
    stream = streams[0]

    try:
        duration_seconds = float(stream.get("duration") or fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        return None

    sample_rate = int(stream.get("sample_rate", 0))
    channels = int(stream.get("channels", 0))
    codec = str(stream.get("codec_name", ""))

    if duration_seconds <= 0 or sample_rate <= 0 or channels <= 0 or not codec:
        return None

    return ProbeOutput(
        duration_ms=int(duration_seconds * 1000),
        sample_rate_hz=sample_rate,
        channels=channels,
        codec=codec,
    )


def validate_duration(
    probe: ProbeOutput | None, *, max_seconds: int
) -> ValidationResult:
    if probe is None:
        return reject(
            ValidationCode.UNPROBEABLE,
            "ffprobe could not probe the audio (corrupt header, unsupported "
            "container, or process timed out).",
        )
    if probe.duration_ms <= max_seconds * 1000:
        return ok()
    return reject(
        ValidationCode.DURATION_EXCEEDED,
        f"audio is {probe.duration_ms / 1000:.1f}s; cap is {max_seconds}s",
    )
