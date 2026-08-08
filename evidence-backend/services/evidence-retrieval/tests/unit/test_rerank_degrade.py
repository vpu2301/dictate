"""rerank_passages: budgeted reranking that degrades to fused order, never 500s."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import uuid4

from evidence_retrieval.domain.rerank import rerank_passages

from evidence_models import EvidencePassage, PassageScores
from models import GatewayError, ModelGatewayClient


def _passage(text: str) -> EvidencePassage:
    return EvidencePassage(
        id=f"chunk:{uuid4()}",
        connector_id="local_corpus@test",
        text=text,
        chunk_id=uuid4(),
        scores=PassageScores(fused=0.02),
    )


class FakeGateway:
    def __init__(
        self,
        *,
        scores_by_text: dict[str, float] | None = None,
        exc: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.calls: list[list[str]] = []
        self._scores_by_text = scores_by_text or {}
        self._exc = exc
        self._delay_s = delay_s

    async def rerank(self, role: str, query: str, texts: list[str]) -> list[float]:
        self.calls.append(list(texts))
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._exc is not None:
            raise self._exc
        return [self._scores_by_text.get(t, 0.0) for t in texts]


def _client(fake: FakeGateway) -> ModelGatewayClient:
    return cast(ModelGatewayClient, fake)


async def test_happy_path_assigns_rerank_scores_in_order() -> None:
    passages = [_passage("alpha"), _passage("beta"), _passage("gamma")]
    gateway = FakeGateway(scores_by_text={"alpha": 1.5, "beta": -0.5, "gamma": 3.0})

    updated, degraded = await rerank_passages(
        _client(gateway), query="q", passages=passages, budget_ms=1000, batch_size=16
    )

    assert degraded is False
    assert [p.scores.rerank for p in updated if p.scores is not None] == [1.5, -0.5, 3.0]
    # Order untouched here — rerank only annotates; finalize does the sorting.
    assert [p.chunk_id for p in updated] == [p.chunk_id for p in passages]


async def test_gateway_error_returns_original_list_degraded() -> None:
    passages = [_passage("alpha"), _passage("beta")]
    gateway = FakeGateway(exc=GatewayError(500, "x", "y"))

    updated, degraded = await rerank_passages(
        _client(gateway), query="q", passages=passages, budget_ms=1000, batch_size=16
    )

    assert degraded is True
    assert updated is passages
    assert all(p.scores is not None and p.scores.rerank is None for p in updated)


async def test_budget_exceeded_returns_original_list_degraded() -> None:
    passages = [_passage("alpha")]
    gateway = FakeGateway(delay_s=2.0)

    updated, degraded = await rerank_passages(
        _client(gateway), query="q", passages=passages, budget_ms=50, batch_size=16
    )

    assert degraded is True
    assert updated is passages


async def test_empty_passages_short_circuit() -> None:
    gateway = FakeGateway()

    updated, degraded = await rerank_passages(
        _client(gateway), query="q", passages=[], budget_ms=1000, batch_size=16
    )

    assert (updated, degraded) == ([], False)
    assert gateway.calls == []


async def test_batching_five_passages_batch_size_two_makes_three_calls() -> None:
    passages = [_passage(f"p{i}") for i in range(5)]
    gateway = FakeGateway(scores_by_text={f"p{i}": float(i) for i in range(5)})

    updated, degraded = await rerank_passages(
        _client(gateway), query="q", passages=passages, budget_ms=1000, batch_size=2
    )

    assert degraded is False
    assert [len(call) for call in gateway.calls] == [2, 2, 1]
    assert gateway.calls == [["p0", "p1"], ["p2", "p3"], ["p4"]]
    assert [p.scores.rerank for p in updated if p.scores is not None] == [0.0, 1.0, 2.0, 3.0, 4.0]
