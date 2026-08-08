"""Stage 3 — number & unit normalization.

Rule-based per-language modules implement word-tagging + pattern
matching. Sprint 5 shipped UK + EN and German joined them with the
dictation language rollout; all three deliberately err on the side of
"pass through unchanged" rather than "normalize aggressively wrong" —
clinical correctness on BP/dosage is the gate.
"""

from __future__ import annotations

import logging
import time

from ..pipeline.base import (
    ProcessingContext,
    StageInput,
    StageOutput,
)
from .artifacts import numeric_artifacts_from_output
from .number_norm_de import _UNITS as _UNITS_DE
from .number_norm_de import normalize_de
from .number_norm_en import _UNITS as _UNITS_EN
from .number_norm_en import normalize_en
from .number_norm_uk import _UNITS as _UNITS_UK
from .number_norm_uk import normalize_uk

# The normalizer's OWN canonical unit vocabulary — imported, never
# re-declared, so the artifact reader cannot drift from what the
# normalizer actually writes.
_CANONICAL_UNITS = {
    "uk": frozenset(_UNITS_UK.values()) | {"мм рт. ст."},
    "en": frozenset(_UNITS_EN.values()) | {"mmHg"},
    "de": frozenset(_UNITS_DE.values()) | {"mmHg", "°C"},
}

logger = logging.getLogger(__name__)


class NumberNormStage:
    """Sprint-05 Stage 3."""

    name = "number_norm"
    runs_on_partials: bool = False

    async def process(self, ctx: ProcessingContext, input: StageInput) -> StageOutput:
        t0 = time.monotonic()
        if ctx.language == "uk":
            new_text = normalize_uk(
                input.text,
                decimal_separator=ctx.decimal_separator,
                bp_separator=ctx.bp_separator,
            )
        elif ctx.language == "de":
            new_text = normalize_de(
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
        artifacts = numeric_artifacts_from_output(
            new_text,
            decimal_separator=ctx.decimal_separator,
            canonical_units=_CANONICAL_UNITS[ctx.language],
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
            numeric_artifacts=artifacts,
            date_artifacts=input.date_artifacts,
        )
