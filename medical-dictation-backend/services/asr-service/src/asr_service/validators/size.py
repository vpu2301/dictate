"""Step 4 — file size ≤ MD_ASR_MAX_UPLOAD_MB.

The check operates on the byte buffer the upload accumulated; the API
layer enforces ``Content-Length`` early via FastAPI's
``MAX_REQUEST_BODY_SIZE`` so a malicious client cannot upload a
multi-GB body to find out at this step that it's rejected.
"""

from __future__ import annotations

from .result import ValidationCode, ValidationResult, ok, reject


def validate_size(size_bytes: int, *, max_mb: int) -> ValidationResult:
    cap = max_mb * 1024 * 1024
    if size_bytes <= cap:
        return ok()
    return reject(
        ValidationCode.SIZE_EXCEEDED,
        f"upload is {size_bytes} bytes; cap is {cap} bytes ({max_mb} MB)",
    )
