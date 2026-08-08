"""Corpus connectors: hybrid dense+lexical with in-connector RRF fusion.

local_corpus searches the GLOBAL partition (nil-uuid tenant, readable by
every tenant per migration 0068); tenant_corpus the caller's own partition.
Both are the same machinery pointed at different partitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg

from evidence_models import ConnectorDescriptor, ConnectorKind, EvidencePassage, SourceAuthority
from evidence_retrieval.adapters.opensearch import LexicalSearch
from evidence_retrieval.adapters.pg import dense_search
from evidence_retrieval.domain.fusion import rrf_fuse
from evidence_retrieval.domain.plan import Filters
from evidence_retrieval.domain.preprocess import PreparedQuery
from evidence_retrieval.domain.registry import SearchManyDefault, SourceHealth
from models import ModelGatewayClient

GLOBAL_TENANT = UUID("00000000-0000-0000-0000-000000000000")


@dataclass
class _Runtime:
    pool: asyncpg.Pool
    lexical: LexicalSearch
    gateway: ModelGatewayClient
    caller_tenant: UUID
    snapshot_members: list[UUID] | None
    pool_per_engine: int
    lexical_available: bool = True  # set False by the service when OS is down
    connector_timeout_ms: int = 600
    # Query vector memo: both corpus connectors share one request — embed once.
    query_vector: list[float] | None = None

    async def vector_for(self, text: str) -> list[float]:
        if self.query_vector is None:
            self.query_vector = (await self.gateway.embed("embed.dense", [text]))[0]
        return self.query_vector


class CorpusConnector(SearchManyDefault):
    """One partition's hybrid search. Not registered directly — the service
    builds per-request instances bound to the caller's tenant + snapshot."""

    def __init__(self, runtime: _Runtime, *, kind: ConnectorKind) -> None:
        self._rt = runtime
        partition_desc = "global corpus" if kind is ConnectorKind.local_corpus else "tenant corpus"
        self.descriptor = ConnectorDescriptor(
            id=f"{kind.value}@v1",
            kind=kind,
            display_name=f"Corpus search ({partition_desc})",
            version="1.0",
            authority_default=(
                SourceAuthority.international
                if kind is ConnectorKind.local_corpus
                else SourceAuthority.tenant
            ),
            needs_egress=False,
            tenant_flaggable=kind is ConnectorKind.tenant_corpus,
            timeout_ms=runtime.connector_timeout_ms,
        )

    @property
    def _partition(self) -> UUID:
        if self.descriptor.kind is ConnectorKind.local_corpus:
            return GLOBAL_TENANT
        return self._rt.caller_tenant

    async def search(self, q: PreparedQuery, k: int, filters: Filters) -> list[EvidencePassage]:
        rt = self._rt
        query_vector = await rt.vector_for(q.normalized)
        dense = await dense_search(
            rt.pool,
            tenant_id=rt.caller_tenant,
            partition_tenant=self._partition,
            query_vector=query_vector,
            k=rt.pool_per_engine,
            filters=filters,
            snapshot_members=rt.snapshot_members,
            kind=self.descriptor.kind,
            connector_id=self.descriptor.id,
        )
        lexical: list[EvidencePassage] = []
        if rt.lexical_available:
            lexical = await rt.lexical.search(
                partition_tenant=self._partition,
                query_text=q.lexical_query,
                k=rt.pool_per_engine,
                filters=filters,
                snapshot_members=rt.snapshot_members,
                kind=self.descriptor.kind,
                connector_id=self.descriptor.id,
            )
        return rrf_fuse(dense, lexical)[:k]

    async def fetch_passage(self, passage_ref: str) -> EvidencePassage:
        from db import tenant_connection

        async with tenant_connection(self._rt.pool, self._rt.caller_tenant) as conn:
            row = await conn.fetchrow(
                """
                SELECT c.id AS chunk_id, c.document_version_id, c.section_path,
                       c.char_start, c.char_end, c.text, d.id AS document_id,
                       d.evidence_tier, d.source_authority, d.published_at,
                       d.license_class, d.retracted
                FROM chunks c
                JOIN document_versions dv ON dv.id = c.document_version_id
                JOIN documents d ON d.id = dv.document_id
                WHERE c.id = $1
                """,
                UUID(passage_ref),
            )
        if row is None:
            raise KeyError(passage_ref)
        from evidence_retrieval.adapters.pg import _row_to_passage

        return _row_to_passage(row, kind=self.descriptor.kind, connector_id=self.descriptor.id)

    async def health(self) -> SourceHealth:
        try:
            await self._rt.pool.fetchval("SELECT 1")
        except Exception as exc:  # noqa: BLE001
            return SourceHealth(ok=False, detail=f"pg: {exc}")
        return SourceHealth(ok=True)
