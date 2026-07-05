"""Stage 2b — spoken-punctuation normalization.

Runs immediately AFTER the ML punctuation stage (Stage 2). Rationale:

- Placing it after Stage 2 means the transformer punctuator never has to
  reason about literal "period"/"крапка" tokens, and any marks it inserted
  next to them are reconciled by :func:`normalize_spoken_punctuation`'s
  render step (a redundant adjacent mark is collapsed).
- The newlines this stage inserts land *after* Stage 2's whitespace-
  collapsing post-edits, so line/paragraph breaks survive intact. The
  remaining stages (number → date → abbreviation → confidence) are all
  regex-based and newline-safe.

Deterministic and cheap, so it also runs on partials for responsive live
dictation.
"""

from __future__ import annotations

import time

from ..pipeline.base import ProcessingContext, StageInput, StageOutput
from .spoken_punctuation import normalize_spoken_punctuation


class SpokenPunctuationStage:
    """Convert spoken punctuation commands the ASR left as words."""

    name = "spoken_punctuation"
    runs_on_partials: bool = True

    async def process(self, ctx: ProcessingContext, input: StageInput) -> StageOutput:
        t0 = time.monotonic()
        new_text = normalize_spoken_punctuation(input.text, ctx.language)
        return StageOutput(
            text=new_text,
            words=input.words,
            confidence_spans=input.confidence_spans,
            voice_commands=input.voice_commands,
            operations=input.operations,
            warnings=input.warnings,
            metadata={
                self.name + ".changed": new_text != input.text,
                self.name + ".latency_ms": (time.monotonic() - t0) * 1000.0,
            },
        )


__all__ = ["SpokenPunctuationStage"]
