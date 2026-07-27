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
        commit_horizon_ms=4000,
        no_speech_prob=0.1,
        last_silence_boundary_ms=5600,
    )
    assert not decisions[0].commit
    assert decisions[0].reason == "too_recent"


def test_no_silence_boundary_not_committed() -> None:
    """Past the revision horizon but with no silence boundary and still
    inside `max_provisional_ms`: stays provisional (don't cut
    mid-utterance)."""
    c = Committer(max_provisional_ms=4000)
    decisions = c.evaluate(
        candidates=[_w("hello", 1000, 1500)],
        now_ms=5000,  # age 3500 ms — past horizon, under the backstop
        commit_horizon_ms=2000,
        no_speech_prob=0.1,
        last_silence_boundary_ms=None,
    )
    assert not decisions[0].commit
    assert decisions[0].reason == "no_silence_boundary"


def test_stale_word_commits_without_silence_boundary() -> None:
    """The backstop: continuous speech with no qualifying pause must not
    stall the transcript forever (sprint-14 fix, ADR-0013 amendment) —
    otherwise a pause-free session finalizes an EMPTY transcript."""
    c = Committer(max_provisional_ms=4000)
    decisions = c.evaluate(
        candidates=[_w("hello", 1000, 1500)],
        now_ms=5600,  # age 4100 ms — past the backstop
        commit_horizon_ms=2000,
        no_speech_prob=0.1,
        last_silence_boundary_ms=None,
    )
    assert decisions[0].commit
    assert decisions[0].reason == "stale_commit"


def test_stale_backstop_does_not_override_hallucination_guard() -> None:
    """A stale word from a high-no-speech window is still dropped: the
    backstop bounds latency, it does not weaken the quality gates."""
    c = Committer(max_provisional_ms=4000)
    decisions = c.evaluate(
        candidates=[_w("ghost", 1000, 1500)],
        now_ms=20000,
        commit_horizon_ms=2000,
        no_speech_prob=0.95,
        last_silence_boundary_ms=None,
    )
    assert not decisions[0].commit
    assert decisions[0].reason == "high_no_speech_prob"


def test_stale_backstop_does_not_override_commit_horizon() -> None:
    """Revisable words never commit, however the backstop is tuned."""
    c = Committer(max_provisional_ms=100)
    decisions = c.evaluate(
        candidates=[_w("recent", 5000, 5500)],
        now_ms=5800,
        commit_horizon_ms=2000,
        no_speech_prob=0.1,
        last_silence_boundary_ms=None,
    )
    assert not decisions[0].commit
    assert decisions[0].reason == "too_recent"


def test_high_no_speech_drops_hallucination() -> None:
    c = Committer()
    decisions = c.evaluate(
        candidates=[_w("ghost", 1000, 1500)],
        now_ms=10000,
        commit_horizon_ms=4000,
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
        commit_horizon_ms=4000,
        no_speech_prob=0.1,
        last_silence_boundary_ms=1600,
    )
    assert decisions[0].commit
