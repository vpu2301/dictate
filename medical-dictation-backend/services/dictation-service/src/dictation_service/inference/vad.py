"""Energy-based VAD over the sliding buffer.

The streaming committer needs a silence-boundary signal to know when
it's safe to graduate a word from PARTIAL to FINAL. We don't want to
spin up the Silero model per window (latency + memory); a cheap
short-term energy threshold is good enough for boundary detection.

For higher-quality VAD on the full session (sprint 14 diarization), we
fall back to the asr-worker's Silero wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE_HZ: int = 16_000
FRAME_MS: int = 20
SAMPLES_PER_FRAME: int = SAMPLE_RATE_HZ * FRAME_MS // 1000


@dataclass(frozen=True, slots=True)
class VadConfig:
    energy_threshold: float = 0.005      # normalised RMS
    min_silence_frames: int = 25         # 500 ms
    smooth_frames: int = 3


def last_silence_boundary_ms(
    pcm: np.ndarray, *, end_ms: int, config: VadConfig = VadConfig()
) -> int | None:
    """Return the timestamp of the most recent silence-boundary (end of
    speech, start of silence) within ``pcm``, or None if no boundary
    has yet been observed.

    The ``end_ms`` argument is the absolute time of the END of ``pcm`` in
    the session's clock; the returned timestamp is in that same clock.
    """
    if pcm.size < SAMPLES_PER_FRAME:
        return None
    n_frames = pcm.size // SAMPLES_PER_FRAME
    frames = pcm[: n_frames * SAMPLES_PER_FRAME].reshape(n_frames, SAMPLES_PER_FRAME)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    is_silence = (rms < config.energy_threshold).astype(np.int8)

    # Walk back: find the most recent run of >= min_silence_frames silence.
    run = 0
    boundary_frame: int | None = None
    for i in range(n_frames - 1, -1, -1):
        if is_silence[i]:
            run += 1
            if run >= config.min_silence_frames:
                # The silence run starts at i; the speech→silence boundary
                # is i + min_silence_frames - 1 frames back from the end.
                # The boundary timestamp is the START of the silence run.
                boundary_frame = i
                break
        else:
            run = 0
    if boundary_frame is None:
        return None
    # Convert frame index to ms within pcm.
    frame_ms = boundary_frame * FRAME_MS
    pcm_duration_ms = n_frames * FRAME_MS
    return end_ms - (pcm_duration_ms - frame_ms)
