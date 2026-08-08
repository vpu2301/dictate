"""rerank backend: cross-encoder scoring of (query, passage) pairs.

Scores are raw cross-encoder logits — callers normalize (the retrieval
service applies a sigmoid before boosts). Serialized like the embedder to
keep CPU behavior predictable.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder


class Reranker:
    def __init__(self, model_source: str, device: str) -> None:
        self._model_source = model_source
        self._device = device
        self._model: CrossEncoder | None = None
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def model_source(self) -> str:
        return self._model_source

    def load(self) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(self._model_source, device=self._device)

    async def score(self, query: str, texts: list[str]) -> list[float]:
        if self._model is None:
            raise RuntimeError("reranker not loaded")
        model = self._model
        pairs = [(query, text) for text in texts]
        async with self._lock:
            scores = await asyncio.to_thread(model.predict, pairs)
        return [float(s) for s in scores]
