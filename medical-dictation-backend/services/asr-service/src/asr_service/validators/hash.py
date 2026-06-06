"""Step 7 — streaming SHA-256 of the upload.

We compute the hash from the buffered bytes at this stage; in a streaming
upload path the hash is fed incrementally and reused here. Either way
the persisted digest matches the bytes that get encrypted and stored.
"""

from __future__ import annotations

import hashlib


def compute_hash(data: bytes) -> bytes:
    """Return the SHA-256 digest of ``data`` (32 raw bytes)."""
    return hashlib.sha256(data).digest()
