"""HTTP client for evidence-retrieval (rule E2: services talk over HTTP).

The corpus call and the web call are the same endpoint with different
`sources` — and the web call additionally carries the `ClinicalIntent`, which
is the only thing `evidence-retrieval`'s web connector will accept as a query
source (QS1).

`intent` is an explicit, optional argument rather than something read off a
plan object, so this adapter never imports the domain (rule E1) and the only
code that can populate it is the caller in `domain/pipeline.py` — which gets
it from `plan.py`, the one module allowed to decide the web is in scope.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from uuid import UUID

import httpx

from evidence_models import ClinicalIntent, ConnectorMeta, EvidencePassage, RetrieveResponse

logger = logging.getLogger(__name__)


class RetrievalUnavailableError(Exception):
    """Retrieval could not serve the request — callers degrade or fail closed."""


class RetrievalClient:
    def __init__(self, *, base_url: str, service_token: str | None, timeout_s: float) -> None:
        headers = {}
        if service_token:
            headers["X-Service-Token"] = service_token
        self._http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout_s)

    async def retrieve(
        self,
        *,
        query: str,
        sources: Sequence[str],
        k: int,
        tenant_id: UUID,
        timeout_s: float,
        locale: str | None = None,
        snapshot_id: UUID | None = None,
        intent: ClinicalIntent | None = None,
    ) -> RetrieveResponse:
        payload: dict[str, object] = {
            "query": query,
            "k": k,
            "sources": list(sources),
            "tenant_id": str(tenant_id),
        }
        if locale:
            payload["locale"] = locale
        if snapshot_id is not None:
            payload["snapshot_id"] = str(snapshot_id)
        if intent is not None:
            payload["intent"] = intent.model_dump(mode="json")
        try:
            response = await self._http.post("/retrieve", json=payload, timeout=timeout_s)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RetrievalUnavailableError(str(exc)) from exc
        return RetrieveResponse.model_validate(response.json())

    async def ready(self) -> bool:
        try:
            response = await self._http.get("/readyz", timeout=2.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self._http.aclose()


def connector_ids(metas: list[ConnectorMeta]) -> list[str]:
    """Connectors that actually contributed, for provenance (FR-5)."""
    return sorted({meta.connector_id for meta in metas if meta.count > 0})


def dedupe_passages(*groups: list[EvidencePassage]) -> list[EvidencePassage]:
    """Merge retrieval results, keeping the first occurrence of each passage.

    Corpus passages are passed first by the caller: when the same content
    exists both in the tenant corpus and on a public page, the curated,
    versioned, snapshot-pinned copy is the one that gets cited.
    """
    merged: list[EvidencePassage] = []
    seen: set[str] = set()
    for group in groups:
        for passage in group:
            key = str(passage.chunk_id or passage.id)
            if key in seen:
                continue
            seen.add(key)
            merged.append(passage)
    return merged
