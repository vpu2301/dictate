"""Synthesis stage: `generator.heavy`, temperature ≤ 0.2, prompt v1.

Tokens stream from the gateway through `SegmentParser`, and each completed
line is bound and handed to `on_segment` immediately — that is what makes
segment-level SSE possible instead of a spinner until generation ends.

Streaming and "reject + retry" pull against each other: you cannot un-send a
segment. The resolution follows the sprint's own failure table (§11):

* **Nothing emitted yet** → a structural failure is fully recoverable. One
  *constrained* retry (the violated rule re-stated, nothing renegotiated),
  then the honest `insufficient_basis` path.
* **Something already emitted** → no retry is possible, so the bad line is
  dropped, `partial_synthesis` is flagged, and the stream closes with what
  validated. "Close with what streamed + flag" is the spec's own posture.

Either way the invariant holds in the direction that matters: a segment that
fails ET2 or cites a block that does not exist is never emitted at all.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from evidence_answer.domain.binder import (
    BinderError,
    EvidenceBlock,
    ParsedSegment,
    SegmentParser,
    bind,
    build_blocks,
    render_blocks,
)
from evidence_models import EvidencePassage, Segment
from models import GatewayError, ModelGatewayClient

logger = logging.getLogger(__name__)

SYNTHESIS_ROLE = "generator.heavy"

# Emitted to the caller as (placement, segment) the moment a line binds.
SegmentSink = Callable[[str, Segment], Awaitable[None]]

_RETRY_SUFFIX = {
    "dangling_citation": (
        "\n\nYour previous output cited an evidence block that does not exist. "
        "Cite ONLY the blocks listed above, using their exact [Sn] markers."
    ),
    "uncited_evidence_segment": (
        "\n\nYour previous output contained an [.. |evidence] line with no [Sn] "
        "citation marker. Every evidence line must cite at least one block; if "
        "you cannot cite a claim, mark it as interpretation or uncertainty."
    ),
    "empty_output": (
        "\n\nYour previous output contained no lines in the required format. "
        "Emit one segment per line as [SUMMARY|kind] text or [DETAIL|kind] text."
    ),
}


class SynthesisUnavailableError(Exception):
    """The generator could not be reached — fail closed (rule CS3)."""


@dataclass(slots=True)
class SynthesisResult:
    summary: list[Segment] = field(default_factory=list)
    detail: list[Segment] = field(default_factory=list)
    blocks: list[EvidenceBlock] = field(default_factory=list)
    attempts: int = 0
    retry_reason: str = ""
    # Set when a structural failure landed after the first segment was already
    # on the wire: the answer is what streamed, plus this flag.
    partial_reason: str = ""
    dropped_lines: list[str] = field(default_factory=list)
    model_id: str = ""
    temperature: float = 0.0

    @property
    def partial(self) -> bool:
        return bool(self.partial_reason)


def build_prompt(template: str, *, question: str, blocks: list[EvidenceBlock]) -> str:
    return template.replace("{question}", question).replace(
        "{evidence_blocks}", render_blocks(blocks)
    )


async def _one_attempt(
    *,
    gateway: ModelGatewayClient,
    prompt: str,
    blocks: list[EvidenceBlock],
    max_tokens: int,
    temperature: float,
    on_segment: SegmentSink | None,
    result: SynthesisResult,
) -> None:
    """Stream one generation, binding and emitting line by line.

    Raises `BinderError` on the first structural failure *if nothing has been
    emitted yet*; otherwise records `partial_reason` and stops.
    """
    parser = SegmentParser()
    emitted = 0

    async def handle(items: list[ParsedSegment]) -> bool:
        """Bind and emit; returns False when the stream must stop."""
        nonlocal emitted
        for item in items:
            try:
                summary, detail = bind([item], blocks)
            except BinderError as exc:
                if emitted == 0:
                    raise
                logger.info(
                    "synthesis.partial_after_emit",
                    extra={"reason": exc.reason, "emitted": emitted},
                )
                result.partial_reason = exc.reason
                return False
            for placement, bucket, target in (
                ("summary", summary, result.summary),
                ("detail", detail, result.detail),
            ):
                for segment in bucket:
                    # Ids are assigned per bind() call, which sees one item at
                    # a time — renumber against the whole answer.
                    numbered = segment.model_copy(
                        update={"id": f"seg-{len(result.summary) + len(result.detail) + 1}"}
                    )
                    target.append(numbered)
                    emitted += 1
                    if on_segment is not None:
                        await on_segment(placement, numbered)
        return True

    try:
        async for token in gateway.generate_stream(
            SYNTHESIS_ROLE, prompt, max_tokens=max_tokens, temperature=temperature
        ):
            if not await handle(parser.feed(token)):
                return
    except GatewayError as exc:
        if emitted == 0:
            raise SynthesisUnavailableError(str(exc)) from exc
        logger.warning("synthesis.stream_aborted", extra={"emitted": emitted})
        result.partial_reason = "generator_stream_aborted"
        return
    await handle(parser.flush())
    result.dropped_lines = parser.dropped_lines
    if emitted == 0:
        raise BinderError("empty_output", "the model produced no parseable segments")


async def synthesize(
    *,
    gateway: ModelGatewayClient,
    template: str,
    question: str,
    passages: list[EvidencePassage],
    max_tokens: int,
    temperature: float,
    on_segment: SegmentSink | None = None,
) -> SynthesisResult:
    """Run synthesis with at most one structural retry.

    Raises `SynthesisUnavailableError` when the generator is unreachable before any
    output, and `BinderError` when output is still structurally invalid after
    the retry. The caller turns both into the `insufficient_basis` path.
    """
    blocks = build_blocks(passages)
    base_prompt = build_prompt(template, question=question, blocks=blocks)
    last_error: BinderError | None = None

    for attempt in range(2):
        result = SynthesisResult(blocks=blocks, attempts=attempt + 1, temperature=temperature)
        prompt = base_prompt
        if attempt == 1 and last_error is not None:
            prompt += _RETRY_SUFFIX.get(last_error.reason, "")
            result.retry_reason = last_error.reason
        try:
            await _one_attempt(
                gateway=gateway,
                prompt=prompt,
                blocks=blocks,
                max_tokens=max_tokens,
                temperature=temperature,
                # The retry must not re-emit: the first attempt emitted
                # nothing (that is the only way we get here).
                on_segment=on_segment,
                result=result,
            )
        except BinderError as exc:
            last_error = exc
            continue
        return result

    assert last_error is not None  # the loop only falls through via BinderError
    raise last_error
