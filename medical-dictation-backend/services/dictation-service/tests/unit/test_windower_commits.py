"""Windower→committer integration: finals must actually be produced.

Regression for the sprint-04 defect found in sprint 14: the committer
was asked for words older than one FULL window while ``integrate()``
only ever offers words from inside that same window, so no candidate
could ever qualify and a session emitted partials forever, finalizing
an empty transcript. Unit-testing the committer alone missed it (those
tests hand it word ages the windower cannot produce), so these tests
drive the REAL StreamingWindower with synthetic Whisper output.
"""

from __future__ import annotations

import numpy as np

from asr_models import Segment, WordTiming
from dictation_service.inference.windower import StreamingWindower


def _word(text: str, start: int, end: int, p: float = 0.95) -> WordTiming:
    return WordTiming(text=text, start_ms=start, end_ms=end, probability=p)


def _segment(words: list[WordTiming]) -> Segment:
    return Segment(
        text=" ".join(w.text for w in words),
        start_ms=words[0].start_ms,
        end_ms=words[-1].end_ms,
        words=words,
        avg_confidence=0.95,
    )


def _silent_tail_pcm(duration_ms: int, speech_ms: int) -> np.ndarray:
    """PCM with speech then silence, so the energy VAD reports a
    silence boundary near the end of the window."""
    n = 16 * duration_ms
    pcm = np.zeros(n, dtype=np.float32)
    speech_n = 16 * speech_ms
    rng = np.random.default_rng(0)
    pcm[:speech_n] = rng.standard_normal(speech_n).astype(np.float32) * 0.2
    return pcm


def test_windower_emits_finals_across_windows() -> None:
    """Two 4 s windows at the ADR-0013 cadence: words from the first
    window's non-overlap head must commit."""
    w = StreamingWindower(base_prompt="", language="uk")

    # Window 1: 0..4000, speech in 0..1500 then silence.
    tick1 = w.integrate(
        window_segments=[_segment([_word("добрий", 200, 700), _word("день", 800, 1400)])],
        window_no_speech_prob=0.1,
        window_start_ms=0,
        window_end_ms=4000,
        infer_seconds=0.2,
        pcm_for_vad=_silent_tail_pcm(4000, 1500),
    )
    # Window 2: 2000..6000 (2 s overlap), same words re-seen plus new.
    tick2 = w.integrate(
        window_segments=[_segment([_word("як", 2200, 2600), _word("справи", 2700, 3300)])],
        window_no_speech_prob=0.1,
        window_start_ms=2000,
        window_end_ms=6000,
        infer_seconds=0.2,
        pcm_for_vad=_silent_tail_pcm(4000, 1500),
    )

    committed = tick1.new_finals + tick2.new_finals
    assert committed, "windower produced no finals — the commit horizon is unreachable again"
    text = " ".join(seg.text for seg in committed)
    assert "добрий" in text and "день" in text


def test_recent_words_stay_provisional() -> None:
    """Words inside the next window's re-transcription range must NOT
    commit — they can still be revised by alignment."""
    w = StreamingWindower(base_prompt="", language="uk")
    tick = w.integrate(
        # Word ends at 3800, only 200 ms before the window end: well
        # inside the 2 s overlap the next window will re-read.
        window_segments=[_segment([_word("пізнє", 3300, 3800)])],
        window_no_speech_prob=0.1,
        window_start_ms=0,
        window_end_ms=4000,
        infer_seconds=0.2,
        pcm_for_vad=_silent_tail_pcm(4000, 3900),
    )
    assert tick.new_finals == []
    assert tick.new_partial is not None
    assert "пізнє" in tick.new_partial.text


def test_high_no_speech_prob_blocks_commit_in_windower() -> None:
    w = StreamingWindower(base_prompt="", language="uk")
    tick = w.integrate(
        window_segments=[_segment([_word("привид", 200, 700)])],
        window_no_speech_prob=0.95,  # hallucination guard
        window_start_ms=0,
        window_end_ms=4000,
        infer_seconds=0.2,
        pcm_for_vad=_silent_tail_pcm(4000, 1000),
    )
    assert tick.new_finals == []
