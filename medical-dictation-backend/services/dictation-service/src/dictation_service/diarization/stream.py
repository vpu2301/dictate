"""Per-session streaming diarization (sprint 14, ADR-0034).

Consumes the same 4 s / 2 s windows as the Whisper windower (ADR-0013)
and maintains a session-absolute speaker timeline. Each window:

    PCM window → Silero speech regions → frontier-clipped ≤1.2 s chunks
    → ECAPA embedding → clustering → SpeakerSegments on the timeline

The already-diarized frontier guarantees each millisecond of audio is
embedded exactly once (the 2 s window overlap only provides VAD
context), so the timeline is non-overlapping and clustering evidence is
never double-counted.

While the clusterer is still bootstrapping (no confirmed 2-way split),
``attribute()`` reports words as *pending* — the FE renders text
immediately and colours it when labels land ("labels may trail text").
When the split lands, earlier chunks are retrospectively relabeled, so
by commit time (≥ one full window later, ADR-0013) finals carry stable
labels. A session where only one voice ever speaks starts labeling S1
after ``single_speaker_after_ms`` of audio.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

import numpy as np

from .attribution import AttributionPolicy, attribute_word
from .clustering import ClusteringConfig, OnlineSpeakerClusterer
from .embedder import EcapaEmbedder
from .vad import SileroSegmenter

SAMPLE_RATE_HZ = 16_000
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SpeakerSegment:
    start_ms: int  # session-absolute
    end_ms: int
    label: str  # "S1" | "S2" | "UNKNOWN"
    confidence: float


@dataclass(frozen=True)
class DiarizationConfig:
    # Chunking of VAD speech regions before embedding.
    chunk_target_ms: int = 1200
    chunk_min_ms: int = 250
    # A single-voice session starts labeling S1 without a 2-way split
    # once this much audio has been diarized.
    single_speaker_after_ms: int = 15_000
    clustering: ClusteringConfig = field(default_factory=ClusteringConfig)
    attribution: AttributionPolicy = field(default_factory=AttributionPolicy)


class DiarizationStream:
    """One instance per conversation session. NOT thread-safe; called
    from the session's single window loop only."""

    def __init__(
        self,
        *,
        embedder: EcapaEmbedder,
        segmenter: SileroSegmenter,
        config: DiarizationConfig | None = None,
    ) -> None:
        self._embedder = embedder
        self._segmenter = segmenter
        self._config = config or DiarizationConfig()
        self._clusterer = OnlineSpeakerClusterer(self._config.clustering)
        self.segments: list[SpeakerSegment] = []
        self.diarized_until_ms: int = 0
        self.last_window_seconds: float = 0.0
        self.relabeled_total: int = 0

    @property
    def speaker_count(self) -> int:
        return self._clusterer.speaker_count

    @property
    def bootstrapped(self) -> bool:
        return self._clusterer.bootstrapped

    def process_window(self, pcm: np.ndarray, *, window_start_ms: int) -> list[SpeakerSegment]:
        """Diarize the not-yet-seen tail of ``pcm`` (whose first sample
        is session-absolute ``window_start_ms``). Returns the newly
        appended segments (relabels mutate ``self.segments`` in place)."""
        t0 = time.perf_counter()
        cfg = self._config
        window_end_ms = window_start_ms + int(pcm.shape[0] * 1000 / SAMPLE_RATE_HZ)
        new: list[SpeakerSegment] = []

        for rel_start, rel_end in self._segmenter.speech_regions(pcm):
            abs_start = window_start_ms + rel_start
            abs_end = window_start_ms + rel_end
            start = max(abs_start, self.diarized_until_ms)  # frontier clip
            if abs_end <= start:
                continue
            if abs_end - start < cfg.chunk_min_ms:
                # Tiny post-frontier tail of a region whose head was
                # embedded last window: same VAD region ⇒ same continuous
                # speech, extend the previous segment instead of guessing.
                if self.segments and start - self.segments[-1].end_ms <= 150:
                    prev = self.segments[-1]
                    self.segments[-1] = replace(prev, end_ms=abs_end)
                continue
            for c_start, c_end in _chunks(start, abs_end, cfg.chunk_target_ms, cfg.chunk_min_ms):
                lo = (c_start - window_start_ms) * SAMPLE_RATE_HZ // 1000
                hi = (c_end - window_start_ms) * SAMPLE_RATE_HZ // 1000
                chunk_pcm = pcm[max(0, lo):hi]
                if chunk_pcm.shape[0] < cfg.chunk_min_ms * SAMPLE_RATE_HZ // 1000:
                    continue
                assignment, relabels = self._clusterer.observe(self._embedder.embed(chunk_pcm))
                for r in relabels:
                    old = self.segments[r.chunk_index]
                    self.segments[r.chunk_index] = replace(
                        old, label=r.label, confidence=r.confidence
                    )
                    self.relabeled_total += 1
                seg = SpeakerSegment(
                    start_ms=c_start,
                    end_ms=c_end,
                    label=assignment.label,
                    confidence=round(assignment.confidence, 4),
                )
                # 1:1 with clusterer chunk indices — required for relabels.
                self.segments.append(seg)
                new.append(seg)

        self.diarized_until_ms = max(self.diarized_until_ms, window_end_ms)
        self.last_window_seconds = time.perf_counter() - t0
        return new

    def attribute(self, start_ms: int, end_ms: int) -> tuple[str | None, float | None]:
        """Speaker for a word timing — see ``attribution.attribute_word``.
        Pending (``None``) until the speaker inventory is trustworthy."""
        if not self._clusterer.bootstrapped and self.diarized_until_ms < self._config.single_speaker_after_ms:
            return None, None
        return attribute_word(
            start_ms,
            end_ms,
            self.segments,
            diarized_until_ms=self.diarized_until_ms,
            policy=self._config.attribution,
        )


def _chunks(start_ms: int, end_ms: int, target_ms: int, min_ms: int) -> list[tuple[int, int]]:
    """Split [start, end) into near-equal chunks ≤ target, each ≥ min.
    A short trailing remainder is folded into the previous chunk."""
    total = end_ms - start_ms
    if total <= 0:
        return []
    if total <= target_ms:
        return [(start_ms, end_ms)] if total >= min_ms else []
    n = max(1, round(total / target_ms))
    size = total / n
    out: list[tuple[int, int]] = []
    for i in range(n):
        lo = start_ms + int(i * size)
        hi = start_ms + int((i + 1) * size) if i < n - 1 else end_ms
        out.append((lo, hi))
    return out
