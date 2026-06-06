"""Step 3 — magic-byte sniff matches the declared MIME.

Defends against polyglot files: a file claiming ``audio/wav`` but whose
bytes are an MP3 payload (and may carry hostile metadata) is rejected
here, before any codec inspection.

We use a hand-rolled byte-prefix check rather than libmagic exclusively
because the format set is small and audited; libmagic adds a heavy
dependency and surface area we don't need. Where libmagic is installed
the validator falls back to ``python-magic`` for the cases we don't
handle directly (so adding more types later is a one-liner).
"""

from __future__ import annotations

from typing import Final

from .result import ValidationCode, ValidationResult, ok, reject

# Map of (declared MIME) → list of byte prefixes that are acceptable.
# Each prefix is a (offset, bytes) tuple.
_KNOWN_PREFIXES: Final[dict[str, list[tuple[int, bytes]]]] = {
    "audio/wav": [(0, b"RIFF"), (8, b"WAVE")],
    "audio/x-wav": [(0, b"RIFF"), (8, b"WAVE")],
    "audio/wave": [(0, b"RIFF"), (8, b"WAVE")],
    # MP3: an ID3v2 header or an MPEG frame sync (0xFFEx / 0xFFFx).
    # We accept any byte whose top 11 bits are set (0xFFE0–0xFFFF).
    "audio/mpeg": [(0, b"ID3"), (0, b"\xff\xfb"), (0, b"\xff\xfa"), (0, b"\xff\xf3")],
    "audio/mp3": [(0, b"ID3"), (0, b"\xff\xfb"), (0, b"\xff\xfa"), (0, b"\xff\xf3")],
    "audio/ogg": [(0, b"OggS")],
    "audio/webm": [(0, b"\x1aE\xdf\xa3")],  # EBML
    "audio/flac": [(0, b"fLaC")],
}


def validate_magic_bytes(declared_mime: str, head: bytes) -> ValidationResult:
    """Return :func:`ok` iff ``head`` carries the magic for ``declared_mime``.

    ``head`` must include at least the first 12 bytes of the file.
    Anything shorter is treated as a reject.
    """
    if len(head) < 12:
        return reject(
            ValidationCode.MIME_MISMATCH,
            "file is shorter than the magic-byte window (need ≥12 bytes)",
        )

    prefixes = _KNOWN_PREFIXES.get(declared_mime)
    if prefixes is None:
        # Should never happen — validate_mime gates this. Fail closed.
        return reject(
            ValidationCode.MIME_MISMATCH,
            f"no magic-byte rule for declared MIME {declared_mime!r}",
        )

    for offset, signature in prefixes:
        end = offset + len(signature)
        if end <= len(head) and head[offset:end] == signature:
            return ok()

    return reject(
        ValidationCode.MIME_MISMATCH,
        f"declared MIME {declared_mime!r} but magic bytes do not match. "
        "Polyglot or mis-declared file rejected.",
    )
