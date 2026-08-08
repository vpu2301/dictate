"""Final scoring (spec D6): authority precedence boost (RR3) + recency decay
(RR4). Boosts reorder presentation — they never delete a source (CS4).

Constants below are v1 and clinically reviewable (sign-off register row);
changing them is a pipeline-version event for the standing gate.
"""

from __future__ import annotations

import math
from datetime import date

from evidence_models import EvidencePassage

# RR3: tenant > national > international > primary literature.
AUTHORITY_MULT: dict[str, float] = {
    "tenant": 1.30,
    "national": 1.20,
    "international": 1.10,
    "primary_literature": 1.00,
    "other": 0.95,
}

# RR4: half-life in years per evidence tier (guideline 3y, RCT 8y per spec).
HALF_LIFE_YEARS: dict[str, float] = {
    "guideline": 3.0,
    "systematic_review": 5.0,
    "rct": 8.0,
    "observational": 8.0,
    "other": 5.0,
}
DECAY_FLOOR = 0.30
# Below the decay of any document younger than ~one half-life: evidence of
# unknown age must never outrank recent dated guidance (live-tuned in S03 —
# at 0.85 an undated abstract beat a dated 2023 protocol).
UNDATED_MULT = 0.60

# How strongly authority/recency modulate the relevance score. Boosts are a
# presentation nudge (RR3: reorder, never delete); at 1.0 they overwhelmed
# sigmoid-squashed rerank logits and INVERTED clear relevance ordering
# (ablation: hybrid+rerank nDCG .53 vs .79 pure — S03 live finding). 0.3
# keeps authority/recency as a tie-breaker-strength influence.
BOOST_WEIGHT = 0.3


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _base_score(passage: EvidencePassage) -> float:
    """Rerank logit (sigmoid-normalized) when present, else fused RRF score
    scaled into a comparable (0,1) range."""
    scores = passage.scores
    if scores is None:
        return 0.0
    if scores.rerank is not None:
        return _sigmoid(scores.rerank)
    # RRF scores live in (0, 2/61]; scale so fused-only results stay well
    # below any reranked score of the same rank order but keep ordering.
    return min(1.0, (scores.fused or 0.0) * 30.0) * 0.5


def recency_mult(published: date | None, tier: str | None, *, today: date) -> float:
    if published is None:
        return UNDATED_MULT
    half_life = HALF_LIFE_YEARS.get(tier or "other", 5.0)
    age_years = max(0.0, (today - published).days / 365.25)
    return max(DECAY_FLOOR, 0.5 ** (age_years / half_life))


def finalize(passages: list[EvidencePassage], *, today: date) -> list[EvidencePassage]:
    """Compute scores.final, sort desc, tie-break on chunk id (determinism)."""
    result: list[EvidencePassage] = []
    for passage in passages:
        authority = passage.source_authority.value if passage.source_authority else "other"
        tier = passage.evidence_tier.value if passage.evidence_tier else None
        boost = AUTHORITY_MULT.get(authority, 0.95) * recency_mult(
            passage.published_at, tier, today=today
        )
        final = _base_score(passage) * (boost**BOOST_WEIGHT)
        scores = passage.scores.model_copy(update={"final": final}) if passage.scores else None
        result.append(passage.model_copy(update={"scores": scores, "score": final}))
    result.sort(key=lambda p: (-(p.score or 0.0), str(p.chunk_id or p.id)))
    return result
