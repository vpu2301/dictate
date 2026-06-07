"""Sequence the 8 validators and short-circuit at the first failure.

The runner is kept thin so each step is independently testable. Callers
typically invoke ``run_all`` once and switch on the returned result.

Steps 1 + 8 (auth and quota) are NOT executed by ``run_all`` — they
require a request/DB context that's natural to invoke at the router
level. The runner runs steps 2–7 which are pure file-shape checks.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import replace

from ..config import settings
from .codec import validate_codec
from .duration import probe_audio, validate_duration
from .hash import compute_hash
from .magic_bytes import validate_magic_bytes
from .mime import validate_mime
from .result import UploadFacts, ValidationResult, ok
from .size import validate_size


async def run_all(
    *,
    mime_type: str,
    payload: bytes,
) -> tuple[ValidationResult, UploadFacts]:
    """Run steps 2–7 on the in-memory payload.

    Returns the validation result plus the facts collected up to that
    point. On failure, the facts struct is partially filled.
    """
    facts = UploadFacts(
        mime_type=mime_type,
        size_bytes=len(payload),
        bytes_buffer=payload,
    )

    r = validate_mime(mime_type)
    if not r.ok:
        return r, facts

    r = validate_magic_bytes(mime_type, payload[:64])
    if not r.ok:
        return r, facts

    r = validate_size(len(payload), max_mb=settings.max_upload_mb)
    if not r.ok:
        return r, facts

    # ffprobe wants a path — write to a tempfile in a private dir.
    with tempfile.NamedTemporaryFile(
        prefix="mdx-asr-",
        suffix=_mime_to_suffix(mime_type),
        delete=False,
    ) as tmp:
        tmp.write(payload)
        tmp_path = tmp.name

    try:
        probe = await probe_audio(
            tmp_path,
            ffprobe_path=settings.ffprobe_path,
            timeout_seconds=settings.ffprobe_timeout_seconds,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    r = validate_duration(probe, max_seconds=settings.max_duration_seconds)
    if not r.ok or probe is None:
        return r, facts

    facts = replace(
        facts,
        duration_ms=probe.duration_ms,
        sample_rate_hz=probe.sample_rate_hz,
        channels=probe.channels,
        codec=probe.codec,
    )

    r = validate_codec(
        probe,
        min_sample_rate_hz=settings.min_sample_rate_hz,
        max_channels=settings.max_channels,
    )
    if not r.ok:
        return r, facts

    facts = replace(facts, sha256=compute_hash(payload))
    return ok(), facts


def _mime_to_suffix(mime: str) -> str:
    return {
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/wave": ".wav",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/ogg": ".ogg",
        "audio/webm": ".webm",
        "audio/flac": ".flac",
    }.get(mime, ".bin")
