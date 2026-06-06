"""8-step upload validation pipeline.

Each step is a function returning :class:`ValidationResult`. The
orchestrator (``run_all``) short-circuits at the first failure and
returns the failing result; on success, returns the accumulated
``UploadFacts`` (mime, codec, duration_ms, …).

Order matters:

1. ``auth``         — bearer + ``asr:write`` scope.
2. ``mime``         — MIME header is in our allow-list.
3. ``magic_bytes``  — file's magic bytes match its claimed MIME.
4. ``size``         — total bytes ≤ MD_ASR_MAX_UPLOAD_MB.
5. ``duration``     — ffprobe duration ≤ MD_ASR_MAX_DURATION_SECONDS.
6. ``codec``        — codec/sample-rate/channels in allow-list.
7. ``hash``         — streaming SHA-256 computed and persisted.
8. ``quota``        — tenant monthly quota not exceeded.

Failures emit RFC 9457 problem-details responses (libs/observability
handler) with a stable ``type`` URI per code.
"""

from .codec import validate_codec
from .duration import validate_duration
from .hash import compute_hash
from .magic_bytes import validate_magic_bytes
from .mime import validate_mime
from .quota import validate_quota
from .result import UploadFacts, ValidationCode, ValidationResult, ok, reject
from .runner import run_all
from .size import validate_size

__all__ = [
    "UploadFacts",
    "ValidationCode",
    "ValidationResult",
    "compute_hash",
    "ok",
    "reject",
    "run_all",
    "validate_codec",
    "validate_duration",
    "validate_magic_bytes",
    "validate_mime",
    "validate_quota",
    "validate_size",
]
