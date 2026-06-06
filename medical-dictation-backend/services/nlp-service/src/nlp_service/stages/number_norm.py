"""Stage 3 — number & unit normalization.

Rule-based per-language modules implement word-tagging + pattern
matching. Sprint 5 ships UK + EN; both deliberately err on the side of
"pass through unchanged" rather than "normalize aggressively wrong" —
clinical correctness on BP/dosage is the gate.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from ..pipeline.base import (
    ProcessingContext,
    Stage,
    StageInput,
    StageOutput,
)
from .number_norm_en import normalize_en
from .number_norm_uk import normalize_uk

logger = logging.getLogger(__name__)


class NumberNormStage:
    """Sprint-05 Stage 3."""

    name = "number_norm"
    runs_on_partials: bool = False

    async def process(
        self, ctx: ProcessingContext, input: StageInput
    ) -> StageOutput:
        t0 = time.monotonic()
        if ctx.language == "uk":
            new_text = normalize_uk(
                input.text,
                decimal_separator=ctx.decimal_separator,
                bp_separator=ctx.bp_separator,
            )
        else:
            new_text = normalize_en(
                input.text,
                decimal_separator=ctx.decimal_separator,
                bp_separator=ctx.bp_separator,
            )
        return StageOutput(
            text=new_text,
            words=input.words,
            confidence_spans=input.confidence_spans,
            voice_commands=input.voice_commands,
            operations=input.operations,
            warnings=input.warnings,
            metadata={
                self.name + ".latency_ms": (time.monotonic() - t0) * 1000.0,
                self.name + ".changed": new_text != input.text,
            },
        )
