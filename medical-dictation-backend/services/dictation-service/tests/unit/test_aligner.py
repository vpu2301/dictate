"""Token-alignment + overlap-merge tests."""

from __future__ import annotations

from asr_models import WordTiming
from dictation_service.inference.aligner import (
    align_overlap,
    normalized_levenshtein,
)


def _w(text: str, start: int, end: int, p: float = 1.0) -> WordTiming:
    return WordTiming(text=text, start_ms=start, end_ms=end, probability=p)


def test_identical_overlaps_zero_uncertainty() -> None:
    prev = [_w("hello", 0, 200), _w("world", 200, 400)]
    curr = [_w("hello", 0, 200), _w("world", 200, 400)]
    res = align_overlap(prev, curr)
    assert res.boundary_uncertainty == 0.0
    assert [w.text for w in res.merged] == ["hello", "world"]


def test_disagreement_chooses_higher_probability() -> None:
    prev = [_w("foo", 0, 200, p=0.4)]
    curr = [_w("bar", 0, 200, p=0.9)]
    res = align_overlap(prev, curr)
    # Single position in each list; aligner treats them as 'sub' and
    # keeps the higher-probability one.
    assert res.merged[0].text == "bar"


def test_high_uncertainty_emits_signal() -> None:
    prev = [_w(t, i * 100, (i + 1) * 100) for i, t in enumerate(["a", "b", "c", "d"])]
    curr = [_w(t, i * 100, (i + 1) * 100) for i, t in enumerate(["w", "x", "y", "z"])]
    res = align_overlap(prev, curr)
    assert res.boundary_uncertainty == 1.0


def test_unaligned_low_probability_dropped() -> None:
    prev: list[WordTiming] = []
    curr = [_w("ghost", 0, 200, p=0.1)]
    res = align_overlap(prev, curr, keep_threshold=0.3)
    assert res.merged == []


def test_unaligned_high_probability_kept() -> None:
    prev: list[WordTiming] = []
    curr = [_w("kept", 0, 200, p=0.9)]
    res = align_overlap(prev, curr, keep_threshold=0.3)
    assert [w.text for w in res.merged] == ["kept"]


def test_normalized_levenshtein_identical() -> None:
    assert normalized_levenshtein(["a", "b", "c"], ["a", "b", "c"]) == 0.0


def test_normalized_levenshtein_disjoint() -> None:
    assert normalized_levenshtein(["a", "b"], ["x", "y"]) == 1.0


def test_normalized_levenshtein_empty() -> None:
    assert normalized_levenshtein([], []) == 0.0
    assert normalized_levenshtein([], ["x"]) == 1.0
