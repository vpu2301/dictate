"""Stage 1 — voice command detection.

The stage delegates to :class:`VoiceCommandMatcher`. Detected matches
are converted to :class:`CommandSlot` (for inspectability) + the
matching words are flagged ``is_voice_command_token=True`` so later
stages (punctuation, numbers, abbreviations) skip them.

Mixed-content splitting: a command in the middle of a segment doesn't
split the segment into multiple records — instead, the words remain a
single list with the command tokens flagged. Downstream renderers can
split if they want, but the pipeline's text/words invariant stays
clean.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import replace

from opentelemetry import metrics

from ..pipeline.base import (
    Operation,
    PipelineWarning,
    ProcessingContext,
    StageInput,
    StageOutput,
    Word,
)
from .operations import operations_for
from .voice_command_matcher import CommandSpec, MatchResult, VoiceCommandMatcher

logger = logging.getLogger(__name__)

# Sprint-13 observability for the anamnesis commands. LABEL DISCIPLINE:
# ``op`` and ``reason`` are closed enums from operations.py — never an
# option value, never a section key with free-text provenance.
_meter = metrics.get_meter("mdx.nlp.commands")
_operations = _meter.create_counter(
    "mdx_nlp_operations_total",
    unit="1",
    description="Frontend operations emitted from detected voice commands",
)


class VoiceCommandStage:
    """Sprint-05 Stage 1.

    Inputs:
      - ``ctx.template_sections`` (optional): used for section-command argument
        resolution.
      - ``input.words``: Whisper-style words with timing + probability.

    Outputs:
      - ``words``: same order, with ``is_voice_command_token`` set on consumed words.
      - ``voice_commands``: ordered list of detected :class:`CommandSlot`.
      - ``operations``: one :class:`Operation` per command.
      - ``text``: with command tokens stripped (so punctuation, numbers,
        and abbreviations don't see them).
    """

    name = "voice_commands"
    runs_on_partials: bool = True

    def __init__(self, *, specs_by_language: dict[str, list[CommandSpec]]) -> None:
        self._specs_by_language = specs_by_language

    async def process(self, ctx: ProcessingContext, input: StageInput) -> StageOutput:
        t0 = time.monotonic()
        specs = self._specs_by_language.get(ctx.language, [])
        matcher = VoiceCommandMatcher(
            specs,
            language=ctx.language,
            template_sections=ctx.template_sections,
        )

        words = list(input.words)
        if not words and input.text:
            # If words aren't supplied (batch path with text-only input),
            # synthesise placeholder words so the matcher has something to
            # walk. Pause-before becomes 0 for synthesised words; the
            # confidence gate also drops to 0 because we have no real
            # probability. So this path effectively disables voice
            # commands on text-only inputs — which is the desired
            # sprint-5 behaviour (commands need timing + probability).
            words = []

        results = matcher.detect(words)
        if not results:
            return StageOutput(
                text=input.text,
                words=tuple(words),
                confidence_spans=input.confidence_spans,
                voice_commands=input.voice_commands,
                operations=input.operations,
                warnings=input.warnings,
                metadata={
                    self.name + ".matches": 0,
                    self.name + ".latency_ms": (time.monotonic() - t0) * 1000,
                },
            )

        consumed: set[int] = set()
        for r in results:
            consumed.update(r.consumed_word_indices)

        new_words = tuple(
            replace(w, is_voice_command_token=True) if i in consumed else w
            for i, w in enumerate(words)
        )
        slots = tuple(r.slot for r in results)
        ops = tuple(operations_for(s) for s in slots)
        for op in ops:
            attrs = {"op": op.op, "language": ctx.language}
            reason = (op.arg or {}).get("reason")
            if reason:
                attrs["reason"] = reason
            _operations.add(1, attrs)

        # Rebuild text from non-command words so later stages don't see commands.
        # Batch mode additionally applies text-shaped ops in place — a spoken
        # «крапка» becomes "." attached at the command's position.
        if ctx.apply_operations_inline:
            non_command_text = _apply_ops_inline(new_words, results)
        else:
            non_command_text = " ".join(
                w.text for i, w in enumerate(new_words) if i not in consumed
            ).strip()

        # Surface ambiguous matches (a different command intent fit the same
        # span) so the FE/clinician can confirm rather than trust the
        # arbitrary longest-first winner.
        ambiguity_warnings = tuple(
            PipelineWarning(
                code="ambiguous_command",
                detail=f"{r.slot.intent} also matched: {', '.join(r.ambiguous_with)}",
                stage=self.name,
            )
            for r in results
            if r.ambiguous_with
        )

        return StageOutput(
            text=non_command_text,
            words=new_words,
            confidence_spans=input.confidence_spans,
            voice_commands=input.voice_commands + slots,
            operations=input.operations + ops,
            warnings=input.warnings + ambiguity_warnings,
            metadata={
                self.name + ".matches": len(results),
                self.name + ".consumed_words": len(consumed),
                self.name + ".latency_ms": (time.monotonic() - t0) * 1000,
            },
        )


def _apply_ops_inline(words: tuple[Word, ...], results: list[MatchResult]) -> str:
    """Rebuild segment text with text-shaped operations applied in place.

    Punctuation attaches to the preceding word (no space); a command with
    nothing before it in the segment renders as the bare mark — the caller
    (asr-service batch enrichment) merges such leading marks into the
    previous segment. Editor-only ops (save_draft, navigate_section, …)
    have no textual form and are dropped from text.
    """
    op_at_first_index: dict[int, Operation] = {}
    consumed: set[int] = set()
    for r in results:
        consumed.update(r.consumed_word_indices)
        op_at_first_index[min(r.consumed_word_indices)] = operations_for(r.slot)

    parts: list[str] = []
    for i, w in enumerate(words):
        if i in consumed:
            op = op_at_first_index.get(i)
            if op is None:
                continue
            if op.op == "insert_punctuation":
                value = (op.arg or {}).get("value", "")
                if parts:
                    parts[-1] += value
                elif value:
                    parts.append(value)
            elif op.op == "insert_line_break" and parts:
                parts[-1] += "\n"
            elif op.op == "insert_paragraph_break" and parts:
                parts[-1] += "\n\n"
            continue
        parts.append(w.text)
    # Join on spaces, then collapse space runs around inserted line breaks.
    text = " ".join(parts).strip()
    return re.sub(r"[ \t]*\n[ \t]*", "\n", text)


__all__ = ["CommandSpec", "VoiceCommandStage"]
