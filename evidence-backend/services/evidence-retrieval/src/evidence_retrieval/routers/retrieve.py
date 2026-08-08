"""POST /retrieve (spec §3) — internal, service-token authenticated.

Pipeline per request: preprocess → per-connector hybrid search (isolated) →
cross-connector merge → rerank (budgeted, degradable) → authority/recency
scoring → deterministic sort. Identity does NOT attach here (rule §7);
evidence-answer carries the user, this hop carries the tenant.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from evidence_models import ClinicalIntent, ConnectorStatus, EvidencePassage, RetrieveResponse
from evidence_retrieval.adapters.pg import snapshot_member_versions
from evidence_retrieval.config import settings
from evidence_retrieval.domain.cache import cache_key
from evidence_retrieval.domain.connectors.corpus import GLOBAL_TENANT, CorpusConnector, _Runtime
from evidence_retrieval.domain.connectors.web import WebSource
from evidence_retrieval.domain.plan import Filters
from evidence_retrieval.domain.preprocess import prepare
from evidence_retrieval.domain.registry import ConnectorRegistry
from evidence_retrieval.domain.rerank import rerank_passages
from evidence_retrieval.domain.scoring import finalize
from evidence_retrieval.main_deps import get_state

logger = logging.getLogger(__name__)
router = APIRouter(tags=["retrieve"])

_KNOWN_SOURCES = {"local_corpus", "tenant_corpus", "web", "pubmed", "guideline_registry", "drug"}


async def require_service_token(
    x_service_token: Annotated[str | None, Header()] = None,
) -> None:
    if settings.service_token is None:
        return  # dev-open; startup WARNING emitted (rule BE7)
    if x_service_token != settings.service_token:
        raise HTTPException(status_code=401, detail="invalid service token")


class RetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=10, ge=1)
    sources: list[str] = ["local_corpus"]
    filters: Filters = Filters()
    snapshot_id: UUID | None = None
    # Internal hop: the caller (evidence-answer) derives this from the user
    # token it holds. Defaults to global-only retrieval.
    tenant_id: UUID = GLOBAL_TENANT
    # QS1 (S04): the ONLY query source the `web` connector will accept. It is
    # de-identified by construction, and without it the web connector answers
    # `unavailable` — raw `query` text never leaves the cluster.
    intent: ClinicalIntent | None = None
    locale: str | None = None
    trace_id: str | None = None


@router.post("/retrieve", dependencies=[Depends(require_service_token)])
async def retrieve(body: RetrieveRequest) -> RetrieveResponse:
    state = get_state()
    if body.k > settings.k_max:
        raise HTTPException(status_code=422, detail=f"k_exceeded: max {settings.k_max}")
    unknown = [s for s in body.sources if s not in _KNOWN_SOURCES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"invalid_filter: unknown sources {unknown}")
    if "tenant_corpus" in body.sources and body.tenant_id == GLOBAL_TENANT:
        raise HTTPException(
            status_code=422, detail="invalid_filter: tenant_corpus needs a tenant_id"
        )

    # Dense engine is mandatory (spec §11).
    try:
        await state.app_pool.fetchval("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="retrieval_unavailable") from exc

    payload = body.model_dump(mode="json")
    key = cache_key(payload, lexicon_version=settings.lexicon_version, pins=state.pins)
    # Web responses are never served from this cache: `accessed_at` is
    # per-request evidence of when we read the page, and replaying a cached
    # one would date-stamp a citation with someone else's fetch.
    cacheable = "web" not in body.sources
    if cacheable:
        cached = await state.cache.get(key)
        if cached is not None:
            return cached

    snapshot_members: list[UUID] | None = None
    if body.snapshot_id is not None:
        snapshot_members = await snapshot_member_versions(
            state.app_pool, tenant_id=body.tenant_id, snapshot_id=body.snapshot_id
        )
        if snapshot_members is None:
            raise HTTPException(status_code=422, detail="invalid_filter: unknown snapshot_id")

    lexical_up = await state.lexical.ping()
    runtime = _Runtime(
        pool=state.app_pool,
        lexical=state.lexical,
        gateway=state.gateway,
        caller_tenant=body.tenant_id,
        snapshot_members=snapshot_members,
        pool_per_engine=settings.candidate_pool_per_engine,
        lexical_available=lexical_up,
        connector_timeout_ms=settings.local_connector_timeout_ms,
    )
    registry = ConnectorRegistry()
    from evidence_models import ConnectorKind

    registry.register(CorpusConnector(runtime, kind=ConnectorKind.local_corpus))
    registry.register(CorpusConnector(runtime, kind=ConnectorKind.tenant_corpus))
    registry.register(
        WebSource(
            client=state.websearch,
            service_token=settings.websearch_service_token,
            tenant_id=str(body.tenant_id),
            intent=body.intent,
            locale=body.locale,
            timeout_ms=settings.web_connector_timeout_ms,
        )
    )
    for stub in state.stubs:
        registry.register(stub)

    prepared = prepare(body.query)
    by_kind, metas = await registry.search_all(
        body.sources, prepared, settings.candidate_pool_per_engine, body.filters
    )

    # Cross-connector merge, dedup on chunk id (tenant partition wins ties —
    # its authority boost should apply).
    merged: dict[str, EvidencePassage] = {}
    for kind in sorted(by_kind, key=lambda s: s != "tenant_corpus"):
        for passage in by_kind[kind]:
            merged.setdefault(str(passage.chunk_id or passage.id), passage)

    def _fused(p: EvidencePassage) -> float:
        return p.scores.fused if p.scores and p.scores.fused is not None else 0.0

    candidates = sorted(merged.values(), key=lambda p: (-_fused(p), str(p.chunk_id or p.id)))

    requested_unavailable = any(m.status is ConnectorStatus.unavailable for m in metas)
    degraded = (not lexical_up) or requested_unavailable
    to_rerank = candidates[: settings.rerank_candidates]
    reranked, rerank_degraded = await rerank_passages(
        state.gateway,
        query=prepared.normalized,
        passages=to_rerank,
        budget_ms=settings.rerank_budget_ms,
        batch_size=settings.rerank_batch_size,
    )
    degraded = degraded or rerank_degraded
    if rerank_degraded:
        state.metrics.rerank_degraded += 1

    final = finalize(
        reranked + candidates[settings.rerank_candidates :], today=datetime.now(tz=UTC).date()
    )[: body.k]

    if not lexical_up:
        for meta in metas:
            if meta.status is ConnectorStatus.ok and meta.kind.value.endswith("corpus"):
                meta.status = ConnectorStatus.degraded

    response = RetrieveResponse(
        passages=final,
        connector_meta=metas,
        degraded=degraded,
        snapshot_id=body.snapshot_id,
        lexicon_version=settings.lexicon_version,
    )
    if cacheable:
        await state.cache.set(key, response)
    return response
