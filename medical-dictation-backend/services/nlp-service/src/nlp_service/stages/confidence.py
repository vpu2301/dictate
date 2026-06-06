"""Stage 6 — confidence annotation.

Produces :class:`ConfidenceSpan` instances over the post-processed text
based on per-word Whisper probabilities. The frontend renders spans
with subtle visual cues so clinicians can spot low-confidence words at
a glance.

Span computation:
1. Project each (non-command) word onto the post-processed text by
   greedy fuzzy lookup. Sprint 5 uses ASCII-case-insensitive prefix
   matching; pilot session will surface any drift cases.
2. Assign a level per word:
   - probability < high_concern_below → high_concern
   - high_concern_below ≤ probability < moderate_below → moderate
   - probability ≥ moderate_below → no annotation
3. Merge adjacent same-level spans separated only by whitespace.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

from ..config import settings
from ..pipeline.base import (
    ConfidenceSpan,
    ProcessingContext,
    Stage,
    StageInput,
    StageOutput,
    Word,
)

logger = logging.getLogger(__name__)


class ConfidenceStage:
    """Sprint-05 Stage 6."""

    name = "confidence"
    runs_on_partials: bool = True

    async def process(
        self, ctx: ProcessingContext, input: StageInput
    ) -> StageOutput:
        t0 = time.monotonic()
        spans = _compute_spans(
            text=input.text,
            words=input.words,
            high_below=settings.confidence_high_concern_below,
            moderate_below=settings.confidence_moderate_below,
        )
        return StageOutput(
            text=input.text,
            words=input.words,
            confidence_spans=tuple(spans),
            voice_commands=input.voice_commands,
            operations=input.operations,
            warnings=input.warnings,
            metadata={
                self.name + ".latency_ms": (time.monotonic() - t0) * 1000.0,
                self.name + ".spans": len(spans),
            },
        )


def _compute_spans(
    *,
    text: str,
    words: tuple[Word, ...],
    high_below: float,
    moderate_below: float,
) -> list[ConfidenceSpan]:
    """Walk the words list, locate each non-command word in ``text``,
    and label by probability. Adjacent same-level spans merged."""
    out: list[ConfidenceSpan] = []
    cursor = 0
    text_lower = text.lower()
    for w in words:
        if w.is_voice_command_token:
            continue
        if w.probability >= moderate_below:
            continue
        level: Literal["high_concern", "moderate"] = (
            "high_concern" if w.probability < high_below else "moderate"
        )
        # Find the word in ``text`` starting from cursor.
        needle = w.text.lower()
        start = text_lower.find(needle, cursor)
        if start == -1:
            # The text was reformatted enough that we can't locate this
            # word — sprint-5 budget accepts the drop; pilot session
            # validates this is rare.
            continue
        end = start + len(needle)
        cursor = end
        # Merge with previous if adjacent + same level.
        if out:
            last = out[-1]
            if last.level == level and text[last.end_char:start].strip() == "":
                out[-1] = ConfidenceSpan(
                    start_char=last.start_char,
                    end_char=end,
                    level=level,
                )
                continue
        out.append(ConfidenceSpan(start_char=start, end_char=end, level=level))
    return out
