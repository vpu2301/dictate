"""embed.dense backend: sentence-transformers on the configured device.

Model load happens once at startup (lifespan); /readyz reports it. Encoding
is normalized so cosine distance in pgvector is meaningful.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


class DenseEmbedder:
    def __init__(self, model_source: str, device: str, expected_dim: int) -> None:
        self._model_source = model_source
        self._device = device
        self._expected_dim = expected_dim
        self._model: SentenceTransformer | None = None
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def model_source(self) -> str:
        return self._model_source

    def load(self) -> None:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(self._model_source, device=self._device)
        dim = model.get_sentence_embedding_dimension()
        if dim != self._expected_dim:
            raise RuntimeError(
                f"embed.dense dimension mismatch: model yields {dim}, "
                f"config/pins expect {self._expected_dim}"
            )
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            raise RuntimeError("embedder not loaded")
        model = self._model
        # Serialized: one encode at a time keeps CPU behavior predictable and
        # memory bounded; batching happens inside encode().
        async with self._lock:
            vectors = await asyncio.to_thread(
                model.encode,
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        return [list(map(float, v)) for v in vectors]
