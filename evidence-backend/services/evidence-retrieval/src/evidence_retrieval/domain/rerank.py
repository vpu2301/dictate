"""Reranking via the gateway 'rerank' role (spec D5).

Budgeted: everything must land inside rerank_budget_ms or the caller keeps
fused order with degraded=true. Batches of rerank_batch_size pairs.
"""

from __future__ import annotations

import asyncio
import logging

from evidence_models import EvidencePassage
from models import GatewayError, ModelGatewayClient

logger = logging.getLogger(__name__)


async def rerank_passages(
    gateway: ModelGatewayClient,
    *,
    query: str,
    passages: list[EvidencePassage],
    budget_ms: int,
    batch_size: int,
) -> tuple[list[EvidencePassage], bool]:
    """Returns (passages_with_rerank_scores, degraded). On timeout or gateway
    failure the ORIGINAL list comes back untouched with degraded=True."""
    if not passages:
        return passages, False

    async def _score_all() -> list[float]:
        scores: list[float] = []
        for start in range(0, len(passages), batch_size):
            batch = passages[start : start + batch_size]
            scores.extend(await gateway.rerank("rerank", query, [p.text for p in batch]))
        return scores

    try:
        scores = await asyncio.wait_for(_score_all(), timeout=budget_ms / 1000)
    except (TimeoutError, GatewayError) as exc:
        logger.warning("rerank.degraded", extra={"reason": type(exc).__name__})
        return passages, True

    updated = [
        p.model_copy(update={"scores": p.scores.model_copy(update={"rerank": s})})
        if p.scores is not None
        else p
        for p, s in zip(passages, scores, strict=True)
    ]
    return updated, False
