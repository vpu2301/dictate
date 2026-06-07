"""Partial-to-final commitment policy.

A word graduates from PARTIAL to FINAL when:

1. Its window-relative end time has slid out of the active window
   (i.e., it's older than one full window's worth of audio); AND
2. A VAD-detected silence boundary lies between this word and the next
   non-silence sample in the buffer (so we don't cut mid-utterance); AND
3. The window's ``no_speech_prob`` ≤ threshold (drops Whisper's
   hallucinated tail-of-silence text).

Once committed, the word is immutable. The session loop appends it to
``transcript_jsonb`` and stops sending it as a partial.
"""

from __future__ import annotations

from dataclasses import dataclass

from asr_models import Segment, WordTiming

from ..config import settings


@dataclass(slots=True)
class CommitDecision:
    """Per-word commit verdict."""

    word: WordTiming
    commit: bool
    reason: str = ""


@dataclass(slots=True)
class Committer:
    """Stateful committer.

    Caller invokes :meth:`evaluate` per window with the current
    candidates and the session-level metadata. The committer marks
    eligible words as final; the rest stay provisional.
    """

    no_speech_threshold: float = settings.no_speech_prob_drop_threshold

    def evaluate(
        self,
        *,
        candidates: list[WordTiming],
        now_ms: int,
        window_seconds: float,
        no_speech_prob: float,
        last_silence_boundary_ms: int | None,
    ) -> list[CommitDecision]:
        decisions: list[CommitDecision] = []
        full_window_ms = int(window_seconds * 1000)
        for w in candidates:
            # Rule 3 — hallucination guard. Drop trailing tokens when
            # the segment-level no-speech probability is high.
            if no_speech_prob > self.no_speech_threshold:
                decisions.append(CommitDecision(word=w, commit=False, reason="high_no_speech_prob"))
                continue
            # Rule 1 — word must be older than a full window.
            if (now_ms - w.end_ms) < full_window_ms:
                decisions.append(CommitDecision(word=w, commit=False, reason="too_recent"))
                continue
            # Rule 2 — silence boundary lies between this word and
            # whatever follows. If we don't have one, we keep it
            # provisional rather than commit mid-utterance.
            if last_silence_boundary_ms is None or last_silence_boundary_ms < w.end_ms:
                decisions.append(CommitDecision(word=w, commit=False, reason="no_silence_boundary"))
                continue
            decisions.append(CommitDecision(word=w, commit=True, reason="ok"))
        return decisions


def words_to_final_segments(words: list[WordTiming]) -> list[Segment]:
    """Group adjacent committed words into one or more :class:`Segment`s.

    Splits on gaps > 500 ms — the same threshold the VAD uses for
    silence smoothing.
    """
    if not words:
        return []
    segments: list[Segment] = []
    bucket: list[WordTiming] = [words[0]]
    for prev, curr in zip(words, words[1:], strict=False):
        if curr.start_ms - prev.end_ms > 500:
            segments.append(_make_segment(bucket))
            bucket = [curr]
        else:
            bucket.append(curr)
    segments.append(_make_segment(bucket))
    return segments


def _make_segment(bucket: list[WordTiming]) -> Segment:
    text = " ".join(w.text for w in bucket).strip()
    avg = sum(w.probability for w in bucket) / len(bucket)
    return Segment(
        text=text,
        start_ms=bucket[0].start_ms,
        end_ms=bucket[-1].end_ms,
        words=bucket,
        avg_confidence=max(0.0, min(1.0, avg)),
    )
