"""Lexical index writer (OpenSearch, ADR-000D).

Index mapping uses ICU analysis (analysis-icu ships with the OpenSearch
distribution): icu_tokenizer + icu_folding covers Ukrainian and English in
one field without language-specific stemmers; S03 layers ranking on top.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from opensearchpy import AsyncOpenSearch, helpers

INDEX_SETTINGS: dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "medical_icu": {
                    "type": "custom",
                    "tokenizer": "icu_tokenizer",
                    "filter": ["icu_folding", "lowercase"],
                }
            }
        },
    },
    "mappings": {
        "properties": {
            "chunk_id": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "document_id": {"type": "keyword"},
            "document_version_id": {"type": "keyword"},
            "section_path": {"type": "text", "analyzer": "medical_icu"},
            "text": {"type": "text", "analyzer": "medical_icu"},
            "language": {"type": "keyword"},
            "source_authority": {"type": "keyword"},
            "evidence_tier": {"type": "keyword"},
            "published_at": {"type": "date", "ignore_malformed": True},
            "retracted": {"type": "boolean"},
            # S03 filter fields (RR2 parity with the SQL side).
            "jurisdiction": {"type": "keyword"},
            "specialty": {"type": "keyword"},
            "license_class": {"type": "keyword"},
            "char_start": {"type": "integer"},
            "char_end": {"type": "integer"},
        }
    },
}


class LexicalIndex:
    def __init__(self, *, url: str, index: str) -> None:
        self._client = AsyncOpenSearch(hosts=[url], verify_certs=False, ssl_show_warn=False)
        self._index = index

    async def aclose(self) -> None:
        await self._client.close()

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:  # noqa: BLE001 — readiness probe, any failure is "down"
            return False

    async def ensure_index(self) -> None:
        exists = await self._client.indices.exists(index=self._index)
        if not exists:
            await self._client.indices.create(index=self._index, body=INDEX_SETTINGS)
        else:
            # Additive mapping updates (new keyword/integer fields) are legal
            # on a live index; existing docs simply lack the fields until the
            # reconciliation reindex (scripts/dev/reindex_lexical.py) runs.
            await self._client.indices.put_mapping(
                index=self._index, body=INDEX_SETTINGS["mappings"]
            )

    async def index_chunks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        document_version_id: UUID,
        language: str | None,
        source_authority: str,
        evidence_tier: str,
        published_at: str | None,
        chunks: list[dict[str, Any]],
        jurisdiction: str | None = None,
        specialty: list[str] | None = None,
        license_class: str | None = None,
        retracted: bool = False,
    ) -> int:
        actions = [
            {
                "_op_type": "index",
                "_index": self._index,
                "_id": str(chunk["id"]),
                "chunk_id": str(chunk["id"]),
                "tenant_id": str(tenant_id),
                "document_id": str(document_id),
                "document_version_id": str(document_version_id),
                "section_path": chunk["section_path"],
                "text": chunk["text"],
                "language": language,
                "source_authority": source_authority,
                "evidence_tier": evidence_tier,
                "published_at": published_at,
                "retracted": retracted,
                "jurisdiction": jurisdiction,
                "specialty": specialty or [],
                "license_class": license_class,
                "char_start": chunk.get("char_start"),
                "char_end": chunk.get("char_end"),
            }
            for chunk in chunks
        ]
        success, _ = await helpers.async_bulk(self._client, actions, raise_on_error=True)
        return int(success)

    async def mark_retracted(self, *, document_id: UUID) -> None:
        await self._client.update_by_query(
            index=self._index,
            body={
                "script": {"source": "ctx._source.retracted = true"},
                "query": {"term": {"document_id": str(document_id)}},
            },
            conflicts="proceed",
        )

    async def count(self) -> int:
        result = await self._client.count(index=self._index)
        return int(result["count"])
