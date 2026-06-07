"""Sliding-window orchestrator for streaming Whisper.

Per-window flow (called every ``window_tick_interval_ms``):

1. If less than ``min_window_for_partial_seconds`` of fresh audio →
   no-op.
2. Otherwise, slice ``[cursor − overlap_s, cursor + window_s)`` from
   the session buffer.
3. Submit to the inference queue.
4. Run :func:`align_overlap` between the new window's overlap region
   and the previous window's overlap region. Pick higher-probability
   tokens per aligned pair.
5. Apply commitment policy: words older than one full window + with a
   silence boundary after them + with no_speech_prob ≤ threshold → FINAL.
6. Return :class:`TickOutput` with partials, finals, and warnings; the
   session loop sends them on the wire.

The windower is stateful per-session; one instance lives in each
:class:`SessionContext`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from asr_models import Segment, WordTiming

from ..config import settings
from .aligner import AlignResult, align_overlap
from .committer import CommitDecision, Committer, words_to_final_segments
from .prompt import build_prompt
from .vad import last_silence_boundary_ms

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WindowSlice:
    """One window's audio slice + bookkeeping for the windower."""

    start_ms: int
    end_ms: int
    pcm: np.ndarray


@dataclass(slots=True)
class TickOutput:
    """One windower tick's results."""

    new_partial: Segment | None = None
    new_finals: list[Segment] = field(default_factory=list)
    boundary_uncertainty: float = 0.0
    no_speech_prob: float = 0.0
    infer_seconds: float = 0.0
    window_start_ms: int = 0
    window_end_ms: int = 0


@dataclass
class StreamingWindower:
    """Stateful per-session windower."""

    base_prompt: str
    language: str
    window_s: float = settings.window_seconds
    overlap_s: float = settings.window_overlap_seconds
    min_partial_s: float = settings.window_min_for_partial_seconds

    # Bookkeeping
    cursor_ms: int = 0  # session-absolute end of last window
    finalized_words: list[WordTiming] = field(default_factory=list)
    last_overlap_words: list[WordTiming] = field(default_factory=list)
    committer: Committer = field(default_factory=Committer)

    def next_slice(self, buffer_total_ms: int) -> WindowSlice | None:
        """Decide if there's enough fresh audio to run a new window."""
        fresh_ms = buffer_total_ms - self.cursor_ms
        if fresh_ms < int(self.min_partial_s * 1000):
            return None
        start_ms = max(0, self.cursor_ms - int(self.overlap_s * 1000))
        end_ms = min(buffer_total_ms, start_ms + int(self.window_s * 1000))
        return WindowSlice(start_ms=start_ms, end_ms=end_ms, pcm=np.zeros(0, dtype=np.float32))

    def build_prompt_for_next_window(self) -> str | None:
        return build_prompt(
            base_prompt=self.base_prompt,
            finalized_words=self.finalized_words,
            max_total_tokens=settings.prompt_max_tokens,
        )

    def integrate(
        self,
        *,
        window_segments: list[Segment],
        window_no_speech_prob: float,
        window_start_ms: int,
        window_end_ms: int,
        infer_seconds: float,
        pcm_for_vad: np.ndarray | None = None,
    ) -> TickOutput:
        """Take a fresh inference result and produce partials + finals."""
        # Project window-relative timestamps into session-absolute time.
        abs_words: list[WordTiming] = []
        for seg in window_segments:
            for w in seg.words:
                abs_words.append(
                    WordTiming(
                        text=w.text,
                        start_ms=w.start_ms + window_start_ms,
                        end_ms=w.end_ms + window_start_ms,
                        probability=w.probability,
                    )
                )

        # Align overlap region with the previous window's overlap.
        overlap_start_ms = window_start_ms
        overlap_end_ms = min(window_end_ms, overlap_start_ms + int(self.overlap_s * 1000))
        prev_overlap_subset = [
            w for w in self.last_overlap_words if overlap_start_ms <= w.start_ms < overlap_end_ms
        ]
        curr_overlap_subset = [
            w for w in abs_words if overlap_start_ms <= w.start_ms < overlap_end_ms
        ]
        non_overlap = [w for w in abs_words if w.start_ms >= overlap_end_ms]

        merged_overlap: AlignResult = align_overlap(prev_overlap_subset, curr_overlap_subset)
        candidates = merged_overlap.merged + non_overlap

        # VAD silence-boundary on the window's own PCM.
        silence_boundary_ms: int | None = None
        if pcm_for_vad is not None and pcm_for_vad.size:
            silence_boundary_ms = last_silence_boundary_ms(pcm_for_vad, end_ms=window_end_ms)

        # Run committer on all candidates.
        decisions: list[CommitDecision] = self.committer.evaluate(
            candidates=candidates,
            now_ms=window_end_ms,
            window_seconds=self.window_s,
            no_speech_prob=window_no_speech_prob,
            last_silence_boundary_ms=silence_boundary_ms,
        )

        new_finals_words = [d.word for d in decisions if d.commit]
        provisional_words = [d.word for d in decisions if not d.commit]
        # Append finalized to running list; never duplicate.
        already_final = {(w.start_ms, w.text) for w in self.finalized_words}
        for w in new_finals_words:
            if (w.start_ms, w.text) not in already_final:
                self.finalized_words.append(w)

        # Build a single partial Segment from the provisional words.
        new_partial: Segment | None = None
        if provisional_words:
            new_partial = Segment(
                text=" ".join(w.text for w in provisional_words),
                start_ms=provisional_words[0].start_ms,
                end_ms=provisional_words[-1].end_ms,
                words=provisional_words,
                avg_confidence=(
                    sum(w.probability for w in provisional_words) / len(provisional_words)
                ),
            )
        new_final_segments = words_to_final_segments(new_finals_words)

        # Advance bookkeeping.
        self.cursor_ms = window_end_ms
        self.last_overlap_words = [
            w for w in abs_words if w.end_ms > window_end_ms - int(self.overlap_s * 1000)
        ]

        return TickOutput(
            new_partial=new_partial,
            new_finals=new_final_segments,
            boundary_uncertainty=merged_overlap.boundary_uncertainty,
            no_speech_prob=window_no_speech_prob,
            infer_seconds=infer_seconds,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
