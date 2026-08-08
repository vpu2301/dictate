"""Lexical retrieval over OpenSearch (degradable engine — down ⇒ dense-only
+ degraded flag). Field boosts: section_path (title-bearing) over body text."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from opensearchpy import AsyncOpenSearch

from evidence_models import (
    ConnectorKind,
    EvidencePassage,
    EvidenceTier,
    LicenseClass,
    SourceAuthority,
)
from evidence_retrieval.domain.plan import Filters, compile_opensearch


class LexicalSearch:
    def __init__(self, *, url: str, index: str) -> None:
        self._client = AsyncOpenSearch(hosts=[url], verify_certs=False, ssl_show_warn=False)
        self._index = index

    async def aclose(self) -> None:
        await self._client.close()

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:  # noqa: BLE001
            return False

    async def search(
        self,
        *,
        partition_tenant: UUID,
        query_text: str,
        k: int,
        filters: Filters,
        snapshot_members: list[UUID] | None,
        kind: ConnectorKind,
        connector_id: str,
    ) -> list[EvidencePassage]:
        filter_clauses = compile_opensearch(filters, tenant_id=partition_tenant)
        if snapshot_members is not None:
            filter_clauses.append(
                {"terms": {"document_version_id": [str(v) for v in snapshot_members]}}
            )
        body: dict[str, Any] = {
            "size": k,
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query_text,
                                "fields": ["section_path^2", "text"],
                                "type": "best_fields",
                            }
                        }
                    ],
                    "filter": filter_clauses,
                }
            },
            # Deterministic ordering: score desc, then _id.
            "sort": [{"_score": "desc"}, {"chunk_id": "asc"}],
            "_source": [
                "chunk_id",
                "document_id",
                "document_version_id",
                "section_path",
                "text",
                "source_authority",
                "evidence_tier",
                "published_at",
                "license_class",
                "char_start",
                "char_end",
                "retracted",
            ],
        }
        result = await self._client.search(index=self._index, body=body)
        passages: list[EvidencePassage] = []
        for hit in result["hits"]["hits"]:
            source = hit["_source"]
            passages.append(
                EvidencePassage(
                    id=source["chunk_id"],
                    connector_id=connector_id,
                    text=source["text"],
                    section_path=source.get("section_path"),
                    document_version_id=UUID(source["document_version_id"]),
                    evidence_tier=EvidenceTier(source["evidence_tier"]),
                    source_authority=SourceAuthority(source["source_authority"]),
                    published_at=(
                        date.fromisoformat(source["published_at"][:10])
                        if source.get("published_at")
                        else None
                    ),
                    score=float(hit["_score"]),
                    chunk_id=UUID(source["chunk_id"]),
                    document_id=UUID(source["document_id"]),
                    source_kind=kind,
                    char_start=source.get("char_start"),
                    char_end=source.get("char_end"),
                    license_class=(
                        LicenseClass(source["license_class"])
                        if source.get("license_class")
                        else None
                    ),
                    retracted=source.get("retracted"),
                )
            )
        return passages
