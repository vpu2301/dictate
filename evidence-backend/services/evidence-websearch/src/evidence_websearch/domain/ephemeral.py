"""Per-query ephemeral index: chunk → embed (`embed.dense`) → Redis, TTL 30 min.

Web passages are *not* corpus. They are never written to `chunks`, never
embedded into the persistent vector index, and never survive their TTL. What
survives a query is the page snapshot (so a reopened answer can render), not
the vectors.

The index is tiny by construction (≤ 6 pages × ≤ 40 chunks), so similarity
runs in-process over the stored vectors rather than in a vector database:
one Redis round trip, no second index to keep consistent, and the whole
structure disappears on TTL expiry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import asdict, dataclass

import redis.asyncio as aioredis

from models import ModelGatewayClient

logger = logging.getLogger(__name__)

EMBED_ROLE = "embed.dense"
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n{2,}")


@dataclass(frozen=True, slots=True)
class EphemeralChunk:
    id: str
    page_url: str
    domain: str
    title: str
    text: str
    char_start: int
    char_end: int


def chunk_text(
    text: str, *, page_url: str, domain: str, title: str, size: int, overlap: int, max_chunks: int
) -> list[EphemeralChunk]:
    """Sentence-aware fixed-window chunking with overlap.

    Offsets are into the extracted text, which is what gets stored as the
    page's `extract_ref` — so a citation's char range stays resolvable on
    reopen without re-fetching anything.
    """
    if not text.strip():
        return []
    pieces = [p for p in _SENTENCE_END.split(text) if p and p.strip()]
    chunks: list[EphemeralChunk] = []
    cursor = 0
    buffer: list[str] = []
    buffer_start = 0
    buffer_len = 0

    def flush(end: int) -> None:
        nonlocal buffer, buffer_len, buffer_start
        if not buffer:
            return
        body = " ".join(buffer).strip()
        if body:
            digest = hashlib.sha256(f"{page_url}:{buffer_start}:{end}".encode()).hexdigest()[:16]
            chunks.append(
                EphemeralChunk(
                    id=f"web:{digest}",
                    page_url=page_url,
                    domain=domain,
                    title=title,
                    text=body,
                    char_start=buffer_start,
                    char_end=end,
                )
            )
        buffer, buffer_len = [], 0

    for piece in pieces:
        index = text.find(piece, cursor)
        if index < 0:
            index = cursor
        cursor = index + len(piece)
        if not buffer:
            buffer_start = index
        buffer.append(piece.strip())
        buffer_len += len(piece)
        if buffer_len >= size:
            flush(cursor)
            if len(chunks) >= max_chunks:
                return chunks
            # Overlap: carry the tail of the emitted chunk into the next one.
            if overlap > 0 and chunks:
                tail = chunks[-1].text[-overlap:]
                buffer = [tail]
                buffer_len = len(tail)
                buffer_start = max(0, chunks[-1].char_end - len(tail))
    flush(cursor)
    return chunks[:max_chunks]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class EphemeralIndex:
    def __init__(
        self,
        *,
        redis: aioredis.Redis,
        gateway: ModelGatewayClient,
        ttl_seconds: int,
        batch_size: int,
    ) -> None:
        self._redis = redis
        self._gateway = gateway
        self._ttl = ttl_seconds
        self._batch = batch_size

    @staticmethod
    def key(tenant_id: str, query: str) -> str:
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:24]
        return f"eva:web:idx:{tenant_id}:{digest}"

    async def build(self, *, key: str, chunks: list[EphemeralChunk]) -> int:
        """Embed and store. Returns the number of indexed chunks."""
        if not chunks:
            return 0
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), self._batch):
            batch = chunks[start : start + self._batch]
            vectors.extend(await self._gateway.embed(EMBED_ROLE, [c.text for c in batch]))
        payload = json.dumps(
            [{"chunk": asdict(c), "vector": v} for c, v in zip(chunks, vectors, strict=True)]
        )
        await self._redis.set(key, payload.encode("utf-8"), ex=self._ttl)
        return len(chunks)

    async def search(self, *, key: str, query: str, k: int) -> list[tuple[EphemeralChunk, float]]:
        raw = await self._redis.get(key)
        if not raw:
            return []
        entries = json.loads(raw)
        query_vector = (await self._gateway.embed(EMBED_ROLE, [query]))[0]
        scored: list[tuple[EphemeralChunk, float]] = []
        for entry in entries:
            chunk = EphemeralChunk(**entry["chunk"])
            scored.append((chunk, _cosine(query_vector, entry["vector"])))
        # Ties broken on chunk id so identical scores order deterministically.
        scored.sort(key=lambda pair: (-pair[1], pair[0].id))
        return scored[:k]

    async def size_bytes(self, key: str) -> int:
        raw = await self._redis.get(key)
        return len(raw) if raw else 0

    async def drop(self, key: str) -> None:
        await self._redis.delete(key)
