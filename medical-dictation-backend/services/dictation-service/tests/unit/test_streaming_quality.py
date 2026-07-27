"""Guards on the streaming ASR quality path.

Background: the conversation/scribe surface produced Ukrainian that was
not merely inaccurate but non-existent as language — invented word-shapes
like "Добро губня" for "Доброго дня". The cause was the model
(dictation-service ran whisper-tiny long after batch ASR moved to
large-v3), which is compose configuration; these tests cover the three
code-level defects that amplified it and that a model swap alone would
leave in place.
"""

from __future__ import annotations

import numpy as np

from asr_models import Segment, WordTiming
from dictation_service.inference.prompt import build_prompt
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


def _pcm_all_speech(duration_ms: int) -> np.ndarray:
    """Unbroken speech — no silence boundary, so the committer holds words
    provisional right up to the end of the session."""
    n = 16 * duration_ms
    rng = np.random.default_rng(1)
    return (rng.standard_normal(n).astype(np.float32) * 0.2).astype(np.float32)


# ── The provisional tail must survive finalize ───────────────────────


def test_flush_provisional_recovers_the_tail() -> None:
    """End-of-session must commit words still inside the revision horizon.

    Nothing will revise them and no further audio will produce the silence
    boundary they wait on, so without a flush they are dropped and the
    persisted transcript stops short of what was said.
    """
    w = StreamingWindower(base_prompt="", language="uk")
    tick = w.integrate(
        # Ends 200 ms before the window end — deep inside the 2 s overlap.
        window_segments=[_segment([_word("останнє", 3300, 3800)])],
        window_no_speech_prob=0.1,
        window_start_ms=0,
        window_end_ms=4000,
        infer_seconds=0.2,
        pcm_for_vad=_pcm_all_speech(4000),
    )
    assert tick.new_finals == [], "precondition: the tail is still provisional"

    flushed = w.flush_provisional()
    assert flushed, "provisional tail was dropped instead of committed"
    assert "останнє" in " ".join(seg.text for seg in flushed)


def test_flush_provisional_is_idempotent_and_never_duplicates() -> None:
    """Finalize paths can overlap; a second flush must add nothing, and a
    word already committed must not come back as a duplicate."""
    w = StreamingWindower(base_prompt="", language="uk")
    w.integrate(
        window_segments=[_segment([_word("раз", 3300, 3800)])],
        window_no_speech_prob=0.1,
        window_start_ms=0,
        window_end_ms=4000,
        infer_seconds=0.2,
        pcm_for_vad=_pcm_all_speech(4000),
    )
    first = w.flush_provisional()
    assert first
    assert w.flush_provisional() == []
    texts = [word.text for word in w.finalized_words]
    assert texts.count("раз") == 1


def test_flush_provisional_on_untouched_windower_is_empty() -> None:
    """A session that failed before its first window has nothing to flush."""
    assert StreamingWindower(base_prompt="", language="uk").flush_provisional() == []


# ── The specialty prompt must reach Whisper exactly once ─────────────


def test_composed_prompt_contains_the_base_prompt_once() -> None:
    """`build_prompt` already prepends the specialty prompt.

    The window loop also passed it to the engine as `prompt`, and the
    engine concatenates its two prompt arguments — so every window's
    initial_prompt opened with the specialty prompt twice. A repeated
    initial_prompt is a Whisper repetition/hallucination trigger and the
    duplicate also consumed the budget meant for decoded context.
    """
    base = "Консультація кардіолога."
    composed = build_prompt(
        base_prompt=base,
        finalized_words=[_word("тиск", 0, 400), _word("нормальний", 500, 1200)],
    )
    assert composed is not None
    assert composed.count(base) == 1
    # The decoded tail is what makes the prompt worth sending at all.
    assert "нормальний" in composed


def test_windower_composed_prompt_is_what_the_engine_receives() -> None:
    """The windower's prompt is self-sufficient: it needs no second base."""
    base = "Консультація кардіолога."
    w = StreamingWindower(base_prompt=base, language="uk")
    w.integrate(
        window_segments=[_segment([_word("добрий", 200, 700), _word("день", 800, 1400)])],
        window_no_speech_prob=0.1,
        window_start_ms=0,
        window_end_ms=4000,
        infer_seconds=0.2,
        pcm_for_vad=_pcm_all_speech(4000),
    )
    prompt = w.build_prompt_for_next_window()
    assert prompt is not None and prompt.count(base) == 1


# ── A CPU-throughput window config must still commit ────────────────


def test_wide_window_config_still_commits_and_flushes() -> None:
    """The CPU deployment widens the window to 30 s / 28 s hop to sustain
    realtime. The commit horizon is the OVERLAP, not the window, so
    commitment must be unaffected by the window width."""
    w = StreamingWindower(
        base_prompt="", language="uk", window_s=30.0, overlap_s=2.0, min_partial_s=28.0
    )
    # Steady state at this config: 30 s window, 28 s hop.
    assert w.next_slice(buffer_total_ms=27_000) is None, "should wait for a full hop"
    first = w.next_slice(buffer_total_ms=28_000)
    assert first is not None and (first.start_ms, first.end_ms) == (0, 28_000)

    tick = w.integrate(
        window_segments=[
            _segment([_word("артеріальний", 1000, 1800), _word("тиск", 1900, 2400)])
        ],
        window_no_speech_prob=0.1,
        window_start_ms=0,
        window_end_ms=28_000,
        infer_seconds=23.5,
        pcm_for_vad=_pcm_all_speech(28_000),
    )
    committed = " ".join(seg.text for seg in tick.new_finals)
    assert "артеріальний" in committed and "тиск" in committed, (
        "a wide window must not stall commitment — the horizon is the overlap"
    )

    # And the hop stays 28 s once the overlap kicks in.
    second = w.next_slice(buffer_total_ms=56_000)
    assert second is not None and (second.start_ms, second.end_ms) == (26_000, 56_000)


# ── The audio tail shorter than one hop must still be transcribed ────


def test_short_tail_needs_a_forced_window() -> None:
    """A remainder below the hop is never offered a window on its own.

    This is the loss that scales with the hop: at the 1.5 s default it is a
    clipped final word, at a 28 s CPU hop it is most of the closing
    exchange of the consultation.
    """
    w = StreamingWindower(
        base_prompt="", language="uk", window_s=30.0, overlap_s=2.0, min_partial_s=28.0
    )
    w.cursor_ms = 28_000  # one window already processed
    # 12 s of trailing audio — real speech, but under the 28 s hop.
    assert w.next_slice(buffer_total_ms=40_000) is None

    forced = w.next_slice(buffer_total_ms=40_000, force=True)
    assert forced is not None, "end-of-session must still window the tail"
    assert forced.end_ms == 40_000, "the forced window must reach the end of the audio"
    assert forced.start_ms == 26_000, "and keep the overlap for alignment"


def test_force_does_not_invent_a_window_when_fully_consumed() -> None:
    """Nothing fresh means no window, forced or not — otherwise finalize
    would spend a full inference on zero samples."""
    w = StreamingWindower(base_prompt="", language="uk")
    w.cursor_ms = 20_000
    assert w.next_slice(buffer_total_ms=20_000, force=True) is None
    assert w.next_slice(buffer_total_ms=19_000, force=True) is None
