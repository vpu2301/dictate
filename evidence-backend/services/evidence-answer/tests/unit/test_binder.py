"""Segment parsing + citation binding — where "the model said" becomes
"the user sees"."""

from __future__ import annotations

import pytest
from evidence_answer.domain.binder import (
    BinderError,
    SegmentParser,
    bind,
    build_blocks,
    cited_block_ids,
    render_blocks,
)

from evidence_models import EvidencePassage, SegmentKind


def passage(idx: int, text: str = "Amoxicillin is first line.") -> EvidencePassage:
    return EvidencePassage(id=f"chunk-{idx}", connector_id="corpus@local", text=text)


BLOCKS = build_blocks([passage(1), passage(2), passage(3)])


def parse(text: str) -> list:
    parser = SegmentParser()
    out = parser.feed(text)
    out.extend(parser.flush())
    return out


# ── parsing ─────────────────────────────────────────────────────────────


def test_parses_placement_kind_and_markers() -> None:
    parsed = parse("[SUMMARY|evidence] Amoxicillin is first line [S1] [S3].\n")

    assert len(parsed) == 1
    assert parsed[0].placement == "summary"
    assert parsed[0].kind == "evidence"
    assert parsed[0].markers == (1, 3)


def test_parser_emits_segments_as_lines_complete_not_at_the_end() -> None:
    """This is what makes SSE segment streaming possible."""
    parser = SegmentParser()

    assert parser.feed("[SUMMARY|evid") == []
    assert parser.feed("ence] First claim [S1].") == []
    first = parser.feed("\n[DETAIL|next_step] Check renal function.")

    assert len(first) == 1
    assert first[0].text.startswith("First claim")
    # The second line has no newline yet — it appears on flush.
    assert len(parser.flush()) == 1


def test_unparseable_lines_are_dropped_but_recorded() -> None:
    parser = SegmentParser()
    parsed = parser.feed("Here is the answer:\n[SUMMARY|evidence] Claim [S1].\n")

    assert len(parsed) == 1
    assert parser.dropped_lines == ["Here is the answer:"]


def test_model_may_not_author_patient_fact_segments() -> None:
    """Quick mode has no patient context; a `patient_fact` line would be a
    fabricated chart fact."""
    parser = SegmentParser()
    parsed = parser.feed("[SUMMARY|patient_fact] The patient is diabetic.\n")

    assert parsed == []
    assert parser.dropped_lines


def test_unknown_kind_is_dropped() -> None:
    parser = SegmentParser()
    assert parser.feed("[SUMMARY|conclusion] Something.\n") == []
    assert parser.dropped_lines


# ── binding ─────────────────────────────────────────────────────────────


def test_markers_bind_to_passage_ids_and_leave_the_text() -> None:
    summary, detail = bind(parse("[SUMMARY|evidence] Amoxicillin first line [S1].\n"), BLOCKS)

    assert len(summary) == 1 and detail == []
    segment = summary[0]
    assert segment.kind is SegmentKind.evidence
    assert [c.source_id for c in segment.citations] == ["S1"]
    assert [c.passage_id for c in segment.citations] == ["chunk-1"]
    # Markers are addressing metadata: they leave the prose entirely, and take
    # their surrounding whitespace with them.
    assert segment.text == "Amoxicillin first line."


def test_marker_removal_leaves_clean_prose_mid_sentence() -> None:
    summary, _ = bind(
        parse("[SUMMARY|evidence] Therapy is X [S1], though duration varies [S2].\n"),
        BLOCKS,
    )
    assert summary[0].text == "Therapy is X, though duration varies."


def test_repeated_marker_produces_one_citation() -> None:
    summary, _ = bind(parse("[SUMMARY|evidence] Claim [S1] and again [S1].\n"), BLOCKS)
    assert len(summary[0].citations) == 1


def test_dangling_marker_is_a_structural_failure() -> None:
    with pytest.raises(BinderError) as excinfo:
        bind(parse("[SUMMARY|evidence] Claim [S9].\n"), BLOCKS)
    assert excinfo.value.reason == "dangling_citation"


def test_uncited_evidence_segment_is_rejected_et2() -> None:
    with pytest.raises(BinderError) as excinfo:
        bind(parse("[SUMMARY|evidence] A confident claim with no source.\n"), BLOCKS)
    assert excinfo.value.reason == "uncited_evidence_segment"


def test_uncited_non_evidence_segments_are_fine() -> None:
    summary, detail = bind(
        parse(
            "[SUMMARY|uncertainty] The evidence is thin here.\n"
            "[DETAIL|missing_info] Renal impairment is not covered.\n"
            "[DETAIL|next_step] Check local resistance data.\n"
        ),
        BLOCKS,
    )
    assert len(summary) == 1
    assert len(detail) == 2


def test_empty_output_is_a_structural_failure() -> None:
    with pytest.raises(BinderError) as excinfo:
        bind(parse("I'm sorry, I cannot help with that.\n"), BLOCKS)
    assert excinfo.value.reason == "empty_output"


def test_summary_and_detail_are_separated() -> None:
    summary, detail = bind(parse("[SUMMARY|evidence] A [S1].\n[DETAIL|evidence] B [S2].\n"), BLOCKS)
    assert len(summary) == 1
    assert len(detail) == 1


# ── sources panel ───────────────────────────────────────────────────────


def test_only_cited_blocks_reach_the_sources_panel() -> None:
    """A retrieved-but-uncited passage stays in provenance, never in
    `sources` — listing it would imply the answer rests on it."""
    summary, detail = bind(parse("[SUMMARY|evidence] A [S2].\n"), BLOCKS)

    assert cited_block_ids(summary, detail) == {"S2"}


# ── prompt rendering ────────────────────────────────────────────────────


def test_rendered_blocks_are_delimited_and_numbered() -> None:
    rendered = render_blocks(BLOCKS)

    assert "<<<EVIDENCE S1" in rendered
    assert "<<<END S3>>>" in rendered
    assert rendered.count("<<<EVIDENCE") == 3


def test_block_text_is_truncated_to_the_cap() -> None:
    long_passage = passage(1, "x" * 5000)
    rendered = render_blocks(build_blocks([long_passage]), max_chars_per_block=100)
    assert rendered.count("x") == 100
