"""DiarizationStream tests with fake embedder/segmenter (no torch, no models).

The fake embedder returns preset unit vectors in call order; the fake
segmenter returns preset window-relative speech regions per call. Vector
geometry mirrors test_diarization_clustering: voice A around e0, voice B
around ``0.2*e0 + sqrt(0.96)*e1`` (cross-voice cosine 0.19).
"""

from __future__ import annotations

import math

import numpy as np

from dictation_service.diarization.stream import (
    SAMPLE_RATE_HZ,
    DiarizationStream,
    _chunks,
)

DIM = 7
_C = math.sqrt(0.95)
_S = math.sqrt(0.05)


class FakeEmbedder:
    def __init__(self, vectors: list[np.ndarray]) -> None:
        self._vectors = list(vectors)
        self.calls = 0

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        v = self._vectors[self.calls]
        self.calls += 1
        return v


class FakeSegmenter:
    """Returns the next preset region list on each speech_regions() call."""

    def __init__(self, regions_per_call: list[list[tuple[int, int]]]) -> None:
        self._regions = list(regions_per_call)
        self.calls = 0

    def speech_regions(self, pcm: np.ndarray) -> list[tuple[int, int]]:
        regions = self._regions[self.calls]
        self.calls += 1
        return regions


def _vec(base: np.ndarray, perturb_axis: int, sign: float = 1.0) -> np.ndarray:
    v = _C * base
    v[perturb_axis] += sign * _S
    return v.astype(np.float32)


def _voice_a_pair() -> list[np.ndarray]:
    a = np.zeros(DIM)
    a[0] = 1.0
    return [_vec(a.copy(), 2, +1.0), _vec(a.copy(), 2, -1.0)]


def _voice_b_pair() -> list[np.ndarray]:
    b = np.zeros(DIM)
    b[0] = 0.2
    b[1] = math.sqrt(1.0 - 0.2 * 0.2)
    return [_vec(b.copy(), 4, +1.0), _vec(b.copy(), 4, -1.0)]


def _same_voice(n: int) -> list[np.ndarray]:
    a = np.zeros(DIM)
    a[0] = 1.0
    return [_vec(a.copy(), 2, +1.0 if i % 2 == 0 else -1.0) for i in range(n)]


def _pcm(ms: int) -> np.ndarray:
    return np.zeros(ms * SAMPLE_RATE_HZ // 1000, dtype=np.float32)


def test_frontier_embeds_each_region_once() -> None:
    embedder = FakeEmbedder(_same_voice(10))
    segmenter = FakeSegmenter([[(0, 4000)], [(0, 4000)]])
    stream = DiarizationStream(embedder=embedder, segmenter=segmenter)  # type: ignore[arg-type]

    # Window 1: 0-4000 ms -> 3 chunks embedded.
    stream.process_window(_pcm(4000), window_start_ms=0)
    assert embedder.calls == 3
    assert stream.diarized_until_ms == 4000

    # Window 2 overlaps 2000 ms already diarized: only 4000-6000 is new.
    stream.process_window(_pcm(4000), window_start_ms=2000)
    assert embedder.calls == 5
    assert stream.diarized_until_ms == 6000

    # Timeline is non-overlapping and monotonically ordered.
    for prev, cur in zip(stream.segments, stream.segments[1:], strict=False):
        assert cur.start_ms >= prev.end_ms
    assert stream.segments[0].start_ms == 0
    assert stream.segments[-1].end_ms == 6000


def test_chunks_splits_long_region_into_near_target_chunks() -> None:
    chunks = _chunks(0, 4000, 1200, 250)
    assert chunks == [(0, 1333), (1333, 2666), (2666, 4000)]
    assert all(hi - lo >= 250 for lo, hi in chunks)
    # Contiguous cover of the region.
    assert chunks[0][0] == 0 and chunks[-1][1] == 4000
    for (_, prev_hi), (lo, _) in zip(chunks, chunks[1:], strict=False):
        assert lo == prev_hi


def test_chunks_short_region_yields_nothing() -> None:
    assert _chunks(0, 200, 1200, 250) == []
    assert _chunks(1000, 1000, 1200, 250) == []


def test_chunks_region_between_min_and_target_is_single_chunk() -> None:
    assert _chunks(500, 1500, 1200, 250) == [(500, 1500)]


def test_tiny_post_frontier_tail_extends_previous_segment() -> None:
    embedder = FakeEmbedder(_same_voice(10))
    segmenter = FakeSegmenter([[(0, 4000)], [(0, 2150)]])
    stream = DiarizationStream(embedder=embedder, segmenter=segmenter)  # type: ignore[arg-type]

    stream.process_window(_pcm(4000), window_start_ms=0)
    assert embedder.calls == 3
    assert stream.segments[-1].end_ms == 4000

    # Window 2 at 2000: region ends 4150 abs, only 150 ms past the
    # frontier (< chunk_min_ms) and contiguous with the last segment.
    new = stream.process_window(_pcm(4000), window_start_ms=2000)
    assert new == []
    assert embedder.calls == 3  # no new embedding
    assert len(stream.segments) == 3
    assert stream.segments[-1].end_ms == 4150


def test_attribute_pending_before_bootstrap_and_single_speaker_regime() -> None:
    embedder = FakeEmbedder(_same_voice(10))
    segmenter = FakeSegmenter([[(0, 4000)]])
    stream = DiarizationStream(embedder=embedder, segmenter=segmenter)  # type: ignore[arg-type]

    stream.process_window(_pcm(4000), window_start_ms=0)
    assert not stream.bootstrapped
    assert stream.diarized_until_ms < stream._config.single_speaker_after_ms
    assert stream.attribute(0, 500) == (None, None)


def test_late_bootstrap_relabels_stored_segments_in_place() -> None:
    vectors = _voice_a_pair() + _voice_b_pair()
    embedder = FakeEmbedder(vectors)
    segmenter = FakeSegmenter([[(0, 1000)]] * 4)
    stream = DiarizationStream(embedder=embedder, segmenter=segmenter)  # type: ignore[arg-type]

    for i in range(4):
        stream.process_window(_pcm(1000), window_start_ms=i * 1000)

    assert stream.bootstrapped
    assert stream.speaker_count == 2
    # Split landed on chunk 3; chunks 0-2 were relabeled retroactively.
    assert [seg.label for seg in stream.segments] == ["S1", "S1", "S2", "S2"]
    assert stream.relabeled_total == 3
    assert all(seg.confidence > 0.0 for seg in stream.segments)

    # Once bootstrapped, attribution works even before the single-speaker
    # regime threshold: a word inside the first chunk maps to S1.
    speaker, conf = stream.attribute(100, 900)
    assert speaker == "S1"
    assert conf is not None and conf > 0.0
