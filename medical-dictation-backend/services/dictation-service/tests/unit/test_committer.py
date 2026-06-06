"""Commitment-policy tests."""

from __future__ import annotations

from asr_models import WordTiming

from dictation_service.inference.committer import Committer


def _w(text: str, start: int, end: int, p: float = 0.9) -> WordTiming:
    return WordTiming(text=text, start_ms=start, end_ms=end, probability=p)


def test_too_recent_word_not_committed() -> None:
    c = Committer()
    decisions = c.evaluate(
        candidates=[_w("hello", 5000, 5500)],
        now_ms=5800,
        window_seconds=4.0,
        no_speech_prob=0.1,
        last_silence_boundary_ms=5600,
    )
    assert not decisions[0].commit
    assert decisions[0].reason == "too_recent"


def test_no_silence_boundary_not_committed() -> None:
    c = Committer()
    decisions = c.evaluate(
        candidates=[_w("hello", 1000, 1500)],
        now_ms=6000,
        window_seconds=4.0,
        no_speech_prob=0.1,
        last_silence_boundary_ms=None,
    )
    assert not decisions[0].commit
    assert decisions[0].reason == "no_silence_boundary"


def test_high_no_speech_drops_hallucination() -> None:
    c = Committer()
    decisions = c.evaluate(
        candidates=[_w("ghost", 1000, 1500)],
        now_ms=10000,
        window_seconds=4.0,
        no_speech_prob=0.9,
        last_silence_boundary_ms=1600,
    )
    assert not decisions[0].commit
    assert decisions[0].reason == "high_no_speech_prob"


def test_commit_when_all_conditions_met() -> None:
    c = Committer()
    decisions = c.evaluate(
        candidates=[_w("hello", 1000, 1500)],
        now_ms=10000,
        window_seconds=4.0,
        no_speech_prob=0.1,
        last_silence_boundary_ms=1600,
    )
    assert decisions[0].commit
