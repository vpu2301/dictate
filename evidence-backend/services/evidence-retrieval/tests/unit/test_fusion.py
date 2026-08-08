"""rrf_fuse: RRF fusion with per-stage score retention and deterministic order."""

from __future__ import annotations

from uuid import UUID, uuid4

from evidence_retrieval.domain.fusion import RRF_K, rrf_fuse

from evidence_models import EvidencePassage


def _passage(chunk_id: UUID, *, score: float | None = None) -> EvidencePassage:
    return EvidencePassage(
        id=f"chunk:{chunk_id}",
        connector_id="local_corpus@test",
        text=f"passage {chunk_id}",
        chunk_id=chunk_id,
        score=score,
    )


def test_passage_in_both_lists_sums_rrf_and_keeps_stage_scores() -> None:
    shared = uuid4()
    dense = [_passage(shared, score=0.9), _passage(uuid4(), score=0.8)]
    lexical = [_passage(uuid4(), score=12.0), _passage(shared, score=7.5)]

    fused = rrf_fuse(dense, lexical)

    winner = next(p for p in fused if p.chunk_id == shared)
    expected = 1.0 / (RRF_K + 1) + 1.0 / (RRF_K + 2)
    assert winner.scores is not None
    assert winner.scores.fused == expected
    assert winner.score == expected
    assert winner.scores.dense == 0.9
    assert winner.scores.lexical == 7.5


def test_dense_only_passage_has_no_lexical_score() -> None:
    dense_only = uuid4()
    fused = rrf_fuse([_passage(dense_only, score=0.7)], [_passage(uuid4(), score=3.0)])

    passage = next(p for p in fused if p.chunk_id == dense_only)
    assert passage.scores is not None
    assert passage.scores.dense == 0.7
    assert passage.scores.lexical is None
    assert passage.scores.fused == 1.0 / (RRF_K + 1)


def test_tie_break_is_deterministic_on_chunk_id() -> None:
    ua = UUID("00000000-0000-0000-0000-00000000000a")
    ub = UUID("00000000-0000-0000-0000-00000000000b")
    # Symmetric ranks: both passages accrue 1/(k+1) + 1/(k+2) → exact tie.
    dense = [_passage(ub), _passage(ua)]
    lexical = [_passage(ua), _passage(ub)]

    fused = rrf_fuse(dense, lexical)

    assert fused[0].scores is not None and fused[1].scores is not None
    assert fused[0].scores.fused == fused[1].scores.fused
    assert [p.chunk_id for p in fused] == [ua, ub]
    # Same inputs, same output — determinism.
    assert [p.chunk_id for p in rrf_fuse(dense, lexical)] == [ua, ub]


def test_empty_lexical_list_degrades_gracefully() -> None:
    ids = [uuid4(), uuid4()]
    fused = rrf_fuse([_passage(i, score=0.5) for i in ids], [])

    assert [p.chunk_id for p in fused] == ids
    for rank, passage in enumerate(fused):
        assert passage.scores is not None
        assert passage.scores.lexical is None
        assert passage.scores.fused == 1.0 / (RRF_K + rank + 1)


def test_result_sorted_by_fused_descending() -> None:
    dense = [_passage(uuid4()) for _ in range(4)]
    lexical = [dense[2], dense[0], _passage(uuid4())]

    fused = rrf_fuse(dense, lexical)

    fused_scores = [p.scores.fused for p in fused if p.scores is not None]
    assert len(fused_scores) == len(fused)
    assert fused_scores == sorted((s for s in fused_scores if s is not None), reverse=True)
