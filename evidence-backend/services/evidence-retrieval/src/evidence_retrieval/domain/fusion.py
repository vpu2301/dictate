"""Reciprocal-rank fusion (rule RR1), k=60, with per-stage score retention."""

from __future__ import annotations

from evidence_models import EvidencePassage, PassageScores

RRF_K = 60


def _key(passage: EvidencePassage) -> str:
    return str(passage.chunk_id or passage.id)


def rrf_fuse(
    dense_ranked: list[EvidencePassage],
    lexical_ranked: list[EvidencePassage],
    *,
    k: int = RRF_K,
) -> list[EvidencePassage]:
    """Fuse two ranked lists; every surviving passage carries dense, lexical
    and fused scores (None where an engine didn't return it). Deterministic:
    ties break on the passage key."""
    merged: dict[str, EvidencePassage] = {}
    fused: dict[str, float] = {}
    dense_scores: dict[str, float | None] = {}
    lexical_scores: dict[str, float | None] = {}

    for rank, passage in enumerate(dense_ranked):
        key = _key(passage)
        merged.setdefault(key, passage)
        fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank + 1)
        dense_scores[key] = passage.score
    for rank, passage in enumerate(lexical_ranked):
        key = _key(passage)
        merged.setdefault(key, passage)
        fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank + 1)
        lexical_scores[key] = passage.score

    result: list[EvidencePassage] = []
    for key, passage in merged.items():
        result.append(
            passage.model_copy(
                update={
                    "scores": PassageScores(
                        dense=dense_scores.get(key),
                        lexical=lexical_scores.get(key),
                        fused=fused[key],
                    ),
                    "score": fused[key],
                }
            )
        )
    result.sort(key=lambda p: (-(p.scores.fused or 0.0), _key(p)))
    return result
