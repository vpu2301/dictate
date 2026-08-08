"""finalize + recency_mult: authority boost (RR3), recency decay (RR4), determinism."""

from __future__ import annotations

import math
from datetime import date
from uuid import UUID, uuid4

import pytest
from evidence_retrieval.domain.scoring import (
    BOOST_WEIGHT,
    DECAY_FLOOR,
    UNDATED_MULT,
    finalize,
    recency_mult,
)

from evidence_models import EvidencePassage, EvidenceTier, PassageScores, SourceAuthority

TODAY = date(2026, 8, 5)


def _passage(
    *,
    chunk_id: UUID | None = None,
    authority: SourceAuthority | None = None,
    tier: EvidenceTier | None = None,
    published_at: date | None = None,
    scores: PassageScores | None = None,
) -> EvidencePassage:
    chunk_id = chunk_id or uuid4()
    return EvidencePassage(
        id=f"chunk:{chunk_id}",
        connector_id="local_corpus@test",
        text="text",
        chunk_id=chunk_id,
        source_authority=authority,
        evidence_tier=tier,
        published_at=published_at,
        scores=scores or PassageScores(fused=0.02, rerank=0.0),
    )


def test_authority_precedence_orders_identical_passages() -> None:
    order = [
        SourceAuthority.tenant,
        SourceAuthority.national,
        SourceAuthority.international,
        SourceAuthority.primary_literature,
        SourceAuthority.other,
    ]
    shuffled = [order[2], order[4], order[0], order[3], order[1]]
    passages = [_passage(authority=a) for a in shuffled]

    result = finalize(passages, today=TODAY)

    assert [p.source_authority for p in result] == order


def test_recency_guideline_three_year_half_life() -> None:
    mult = recency_mult(date(2023, 8, 5), "guideline", today=TODAY)
    assert mult == pytest.approx(0.5, rel=1e-2)


def test_recency_rct_eight_year_half_life() -> None:
    mult = recency_mult(date(2018, 8, 5), "rct", today=TODAY)
    assert mult == pytest.approx(0.5, rel=1e-2)


def test_recency_floor_for_ancient_documents() -> None:
    assert recency_mult(date(1996, 8, 5), "guideline", today=TODAY) == DECAY_FLOOR
    assert recency_mult(date(1996, 8, 5), "rct", today=TODAY) == DECAY_FLOOR


def test_undated_gets_undated_mult_and_ranks_below_recent_dated() -> None:
    assert recency_mult(None, "guideline", today=TODAY) == UNDATED_MULT
    assert UNDATED_MULT == 0.60

    undated = _passage(tier=EvidenceTier.guideline, published_at=None)
    dated = _passage(tier=EvidenceTier.guideline, published_at=date(2025, 8, 5))

    result = finalize([undated, dated], today=TODAY)

    assert result[0].chunk_id == dated.chunk_id
    assert result[1].chunk_id == undated.chunk_id


def test_rerank_logit_sigmoid_normalized_ordering() -> None:
    logits = [0.0, -2.0, 2.0]
    passages = [_passage(scores=PassageScores(fused=0.02, rerank=lg)) for lg in logits]

    result = finalize(passages, today=TODAY)

    assert [p.scores.rerank for p in result if p.scores is not None] == [2.0, 0.0, -2.0]
    top = result[0]
    assert top.scores is not None and top.scores.final is not None
    sigmoid = 1.0 / (1.0 + math.exp(-2.0))
    # authority defaulted to "other" (0.95), undated (0.60), boost dampened by BOOST_WEIGHT.
    assert top.scores.final == pytest.approx(sigmoid * (0.95 * UNDATED_MULT) ** BOOST_WEIGHT)


def test_fused_only_fallback_yields_finite_score() -> None:
    passage = _passage(scores=PassageScores(fused=0.0161))

    (result,) = finalize([passage], today=TODAY)

    assert result.scores is not None
    assert result.scores.final is not None
    assert math.isfinite(result.scores.final)
    assert result.scores.final > 0.0


def test_tie_break_deterministic_on_chunk_id() -> None:
    ua = UUID("00000000-0000-0000-0000-00000000000a")
    ub = UUID("00000000-0000-0000-0000-00000000000b")
    passages = [_passage(chunk_id=ub), _passage(chunk_id=ua)]

    result = finalize(passages, today=TODAY)

    assert result[0].score == result[1].score
    assert [p.chunk_id for p in result] == [ua, ub]
    assert [p.chunk_id for p in finalize(passages, today=TODAY)] == [ua, ub]


def test_final_populated_on_every_output() -> None:
    passages = [
        _passage(scores=PassageScores(fused=0.03, rerank=1.0)),
        _passage(scores=PassageScores(fused=0.01)),
        _passage(scores=PassageScores(fused=0.02, rerank=-1.0), published_at=date(2020, 1, 1)),
    ]

    result = finalize(passages, today=TODAY)

    assert len(result) == len(passages)
    for passage in result:
        assert passage.scores is not None
        assert passage.scores.final is not None
        assert passage.score == passage.scores.final
