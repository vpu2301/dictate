"""Segment parser + citation binder.

The model emits one segment per line in the grammar of `synthesis_v1.md`:

    [SUMMARY|evidence] Amoxicillin is first line for mild CAP [S1] [S3].

`SegmentParser` turns that stream into `Segment` objects **as lines complete**,
which is what makes SSE segment-level streaming possible without waiting for
the whole generation.

The binder is the safety gate between "the model said" and "the user sees":

* a `[Sn]` marker with no matching evidence block is **dangling** — the model
  cited something that does not exist;
* an `evidence` segment with no citation violates ET2;
* both are *structural* failures: one constrained retry, then the honest
  `insufficient_basis` path. They are never repaired by dropping the marker,
  because a claim whose support we cannot name is a claim we cannot show.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from evidence_models import Citation, EvidencePassage, Segment, SegmentKind

# [PLACEMENT|kind] text
_LINE = re.compile(r"^\s*\[(SUMMARY|DETAIL)\s*\|\s*([a-z_]+)\]\s*(.+?)\s*$", re.IGNORECASE)
# Citation marker: [S1], [S12]. Deliberately strict — [S1, S2] and [1] are not
# accepted, so a near-miss surfaces as a parse failure rather than a silent
# uncited claim.
_MARKER = re.compile(r"\[S(\d+)\]")

# Whitespace left behind after markers are lifted out of the prose: runs of
# spaces, and the space that used to sit between a word and its marker but
# now sits between a word and a full stop.
_SPACE_RUN = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?)\]»])")

_KINDS = {kind.value for kind in SegmentKind}
# The model may not author these two: `patient_fact` has no patient context in
# quick mode, and inventing one would fabricate a chart fact.
_MODEL_AUTHORABLE = _KINDS - {SegmentKind.patient_fact.value}


class BinderError(Exception):
    """A structural defect in the model's output. Carries a machine reason so
    the trace records WHY the retry happened."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ParsedSegment:
    placement: str  # "summary" | "detail"
    kind: str
    text: str
    markers: tuple[int, ...]


@dataclass(slots=True)
class SegmentParser:
    """Incremental line parser over a token stream.

    Unparseable lines are collected rather than raised on: a stray preamble
    line ("Here is the answer:") should not kill an otherwise good answer, but
    it must be visible, so `dropped_lines` rides into the trace.
    """

    buffer: str = ""
    dropped_lines: list[str] = field(default_factory=list)

    def feed(self, chunk: str) -> list[ParsedSegment]:
        self.buffer += chunk
        out: list[ParsedSegment] = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            parsed = self._parse_line(line)
            if parsed is not None:
                out.append(parsed)
        return out

    def flush(self) -> list[ParsedSegment]:
        """Parse whatever is left when the stream ends without a newline."""
        remainder, self.buffer = self.buffer, ""
        parsed = self._parse_line(remainder)
        return [parsed] if parsed is not None else []

    def _parse_line(self, line: str) -> ParsedSegment | None:
        if not line.strip():
            return None
        match = _LINE.match(line)
        if match is None:
            self.dropped_lines.append(line.strip()[:200])
            return None
        placement, kind, text = match.group(1).lower(), match.group(2).lower(), match.group(3)
        if kind not in _MODEL_AUTHORABLE:
            self.dropped_lines.append(line.strip()[:200])
            return None
        markers = tuple(int(m) for m in _MARKER.findall(text))
        return ParsedSegment(placement=placement, kind=kind, text=text.strip(), markers=markers)


def _strip_markers(text: str) -> str:
    """Remove `[Sn]` markers and the whitespace they leave behind."""
    without = _MARKER.sub("", text)
    without = _SPACE_RUN.sub(" ", without)
    return _SPACE_BEFORE_PUNCT.sub(r"\1", without).strip()


@dataclass(frozen=True, slots=True)
class EvidenceBlock:
    """One `[Sn]` block as rendered into the prompt."""

    index: int
    source_id: str
    passage: EvidencePassage


def build_blocks(passages: list[EvidencePassage]) -> list[EvidenceBlock]:
    """Number the passages S1..Sn in the order they are shown to the model.

    Multiple passages from the same document each get their own block: the
    citation must resolve to the *passage* that supports the claim, not to the
    document in general (rule SC1, claim → passage in one interaction).
    """
    return [
        EvidenceBlock(index=i + 1, source_id=f"S{i + 1}", passage=p) for i, p in enumerate(passages)
    ]


def render_blocks(blocks: list[EvidenceBlock], *, max_chars_per_block: int = 2400) -> str:
    """Delimited evidence blocks (rule LM4).

    The fence is unambiguous and the block header names the source, so a page
    that tries to impersonate the instruction section is visibly inside data.
    """
    rendered: list[str] = []
    for block in blocks:
        passage = block.passage
        origin = passage.web_ref.domain if passage.web_ref else (passage.section_path or "corpus")
        text = passage.text.strip()[:max_chars_per_block]
        rendered.append(
            f'<<<EVIDENCE {block.source_id} source="{origin}">>>\n{text}\n<<<END {block.source_id}>>>'
        )
    return "\n\n".join(rendered)


def bind(
    parsed: list[ParsedSegment], blocks: list[EvidenceBlock]
) -> tuple[list[Segment], list[Segment]]:
    """Resolve markers to citations. Returns (summary_segments, detail_segments).

    Raises `BinderError` on a dangling marker or an uncited `evidence` segment.
    """
    by_index = {block.index: block for block in blocks}
    summary: list[Segment] = []
    detail: list[Segment] = []
    counter = 0

    for item in parsed:
        citations: list[Citation] = []
        seen: set[str] = set()
        for marker in item.markers:
            block = by_index.get(marker)
            if block is None:
                raise BinderError(
                    "dangling_citation",
                    f"segment cites [S{marker}] but only "
                    f"{len(blocks)} evidence blocks were supplied",
                )
            if block.source_id in seen:
                continue
            seen.add(block.source_id)
            citations.append(
                Citation(
                    source_id=block.source_id,
                    passage_id=block.passage.id,
                    quote=None,
                )
            )
        if item.kind == SegmentKind.evidence.value and not citations:
            raise BinderError(
                "uncited_evidence_segment",
                "ET2: an 'evidence' segment must carry at least one citation",
            )
        counter += 1
        segment = Segment(
            id=f"seg-{counter}",
            kind=SegmentKind(item.kind),
            # The markers are addressing metadata, not prose: they leave the
            # rendered text and live in `citations` (the SPA draws its own
            # chips from there).
            text=_strip_markers(item.text),
            citations=citations,
        )
        (summary if item.placement == "summary" else detail).append(segment)

    if not summary and not detail:
        raise BinderError("empty_output", "the model produced no parseable segments")
    return summary, detail


def cited_block_ids(summary: list[Segment], detail: list[Segment]) -> set[str]:
    """Source ids actually cited — the sources panel lists only these.

    Retrieved-but-uncited passages stay in provenance (they were considered)
    but never in `sources`, which would otherwise imply the answer rests on
    material it never used.
    """
    return {citation.source_id for segment in [*summary, *detail] for citation in segment.citations}
