"""Per-session Opus decoder.

Opus is a stateful codec; the decoder accumulates inter-frame state. One
:class:`OpusDecoder` instance per session — never shared across sessions.

The implementation imports ``opuslib`` lazily so the module can be
imported on macOS (where the library wheel isn't published) for unit
tests that exercise the surrounding framing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

# Wire constants for the dictation client (sprint 04 frontend):
SAMPLE_RATE_HZ: int = 16_000
CHANNELS: int = 1
FRAME_MS: int = 20
SAMPLES_PER_FRAME: int = SAMPLE_RATE_HZ * FRAME_MS // 1000  # 320
# A 20-ms Opus frame at 16-kHz mono VOIP profile is typically 60-90
# bytes; 1500 bytes is the absolute upper bound. Frames above that are
# rejected at the codec layer (the wire DoS check is 8 KB).


class OpusDecodeError(Exception):
    def __init__(self, detail: str, *, fatal: bool = False) -> None:
        super().__init__(detail)
        self.fatal = fatal


@dataclass
class _DecodeStats:
    frames_ok: int = 0
    frames_failed: int = 0
    consecutive_failures: int = 0


class OpusDecoder:
    """Stateful per-session Opus → PCM (float32 mono 16 kHz).

    ``decode(bytes) -> np.ndarray`` returns 320 float32 samples per
    20-ms frame, normalised to ``[-1, 1]``.

    Five consecutive decode failures raises :class:`OpusDecodeError(fatal=True)`;
    the session loop translates that to a `worker_failed` close.
    """

    _MAX_CONSECUTIVE_FAILURES: int = 5

    def __init__(self) -> None:
        self._decoder: object | None = None
        self._stats = _DecodeStats()
        self._load()

    def _load(self) -> None:
        try:
            import opuslib

            self._decoder = opuslib.Decoder(SAMPLE_RATE_HZ, CHANNELS)
        except Exception as exc:  # noqa: BLE001
            # On macOS / dev hosts without libopus we fall back to a
            # stub decoder that returns silence; tests for upstream
            # logic still run. Production images ship libopus0.
            logger.warning(
                "opus.unavailable_fallback",
                extra={"error": str(exc), "error_class": type(exc).__name__},
            )
            self._decoder = None

    def decode(self, opus_bytes: bytes) -> np.ndarray:
        """Decode one 20-ms Opus packet → 320 float32 samples."""
        if self._decoder is None:
            # Stub path — return silence so framing tests still progress.
            self._stats.frames_ok += 1
            self._stats.consecutive_failures = 0
            return np.zeros(SAMPLES_PER_FRAME, dtype=np.float32)

        try:
            # opuslib returns bytes of int16 little-endian samples.
            pcm_bytes = self._decoder.decode(opus_bytes, SAMPLES_PER_FRAME, decode_fec=False)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — codec exceptions are opaque
            self._stats.frames_failed += 1
            self._stats.consecutive_failures += 1
            fatal = self._stats.consecutive_failures >= self._MAX_CONSECUTIVE_FAILURES
            raise OpusDecodeError(
                f"opus decode failed: {type(exc).__name__}: {exc}",
                fatal=fatal,
            ) from exc

        if len(pcm_bytes) != SAMPLES_PER_FRAME * 2:
            self._stats.frames_failed += 1
            raise OpusDecodeError(
                f"opus decode produced {len(pcm_bytes)} bytes, expected {SAMPLES_PER_FRAME * 2}",
            )

        pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        self._stats.frames_ok += 1
        self._stats.consecutive_failures = 0
        return pcm

    @property
    def consecutive_failures(self) -> int:
        return self._stats.consecutive_failures

    @property
    def frames_ok(self) -> int:
        return self._stats.frames_ok

    @property
    def frames_failed(self) -> int:
        return self._stats.frames_failed
