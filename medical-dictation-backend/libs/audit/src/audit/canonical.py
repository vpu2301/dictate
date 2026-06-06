"""RFC 8785 JSON Canonicalization Scheme (JCS).

We delegate to the vetted ``rfc8785`` library rather than implementing the
spec ourselves. JCS gives us:

- Lexicographically sorted object keys.
- Numbers in shortest valid JSON Number form.
- Strings with sorted Unicode escapes.
- Arrays preserved.

Why we need it: the audit hash chain commits ``sha256(prev_hash || jcs(event))``.
If two callers produce the same logical event with different key insertion
order, JCS guarantees they hash identically — so the writer and the
verifier (Day 5) agree on the chain.

Callers must pre-convert non-JSON-natural types (UUID → str, datetime →
ISO 8601 str, bytes → base64 str) before invoking. JCS rejects NaN and
Infinity per spec.
"""

from __future__ import annotations

from typing import Any

import rfc8785

from .exceptions import CanonicalizationError


def canonicalize(value: Any) -> bytes:
    """Return the RFC 8785 canonical bytes of ``value``.

    Raises:
        CanonicalizationError: ``value`` contains a non-serialisable type
            or a JCS-rejected number (NaN / Infinity).
    """
    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError) as exc:
        raise CanonicalizationError(f"JCS canonicalization failed: {exc}") from exc


def canonicalize_str(value: Any) -> str:
    """Convenience: the canonical bytes as a UTF-8 string."""
    return canonicalize(value).decode("utf-8")
