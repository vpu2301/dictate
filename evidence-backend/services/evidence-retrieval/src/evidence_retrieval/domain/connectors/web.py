"""The `web` connector (EVA-S04) — replaces the S03 `unavailable` stub.

Rule E2 forbids importing another service, so this is an HTTP adapter over
`evidence-websearch`'s internal `POST /web/search`. It satisfies the frozen
`EvidenceSource` protocol unchanged (the freeze suite proves it).

**QS1 lives in this class's precondition.** The web is reachable only when the
caller supplied an explicit `ClinicalIntent` on the request: no intent, no web
search, `unavailable` with a reason. A `PreparedQuery` is just text — it could
be a question, and from S05 on it could be a question enriched with patient
context — so it is deliberately NOT accepted as a fallback query source. The
only door to the internet requires a de-identified concept list to open it.
"""

from __future__ import annotations

import logging

import httpx

from evidence_models import ClinicalIntent, ConnectorDescriptor, ConnectorKind, EvidencePassage
from evidence_retrieval.domain.plan import Filters
from evidence_retrieval.domain.preprocess import PreparedQuery
from evidence_retrieval.domain.registry import (
    SearchManyDefault,
    SourceHealth,
    SourceUnavailableError,
)

logger = logging.getLogger(__name__)


class WebSource(SearchManyDefault):
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        service_token: str | None,
        tenant_id: str,
        intent: ClinicalIntent | None,
        locale: str | None,
        timeout_ms: int,
    ) -> None:
        self.descriptor = ConnectorDescriptor(
            id="web@evidence-websearch",
            kind=ConnectorKind.web,
            display_name="Live web (allowlisted medical domains)",
            version="1.0",
            enabled=True,
            needs_egress=True,
            tenant_flaggable=True,
            timeout_ms=timeout_ms,
        )
        self._client = client
        self._service_token = service_token
        self._tenant_id = tenant_id
        self._intent = intent
        self._locale = locale

    async def search(self, q: PreparedQuery, k: int, filters: Filters) -> list[EvidencePassage]:
        if self._intent is None:
            raise SourceUnavailableError(
                "web retrieval requires a ClinicalIntent on the request (QS1): "
                "raw query text is never sent to the internet"
            )
        headers = {}
        if self._service_token:
            headers["X-Service-Token"] = self._service_token
        try:
            response = await self._client.post(
                "/web/search",
                headers=headers,
                json={
                    "intent": self._intent.model_dump(mode="json"),
                    "tenant_id": self._tenant_id,
                    "k": k,
                    "locale": self._locale or q.lang_guess,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(f"evidence-websearch unreachable: {exc}") from exc
        payload = response.json()
        if payload.get("degraded"):
            logger.info("web.degraded", extra={"reason": payload.get("degrade_reason", "unknown")})
        return [EvidencePassage.model_validate(p) for p in payload.get("passages", [])]

    async def fetch_passage(self, passage_ref: str) -> EvidencePassage:
        # Web passages are re-read from the answer's stored envelope + page
        # snapshot, never re-fetched (AC-S04-B-6). There is nothing to look up
        # here, and pretending otherwise would invite a re-fetch path.
        raise SourceUnavailableError(
            "web passages are served from the answer snapshot, not re-fetched"
        )

    async def health(self) -> SourceHealth:
        try:
            response = await self._client.get("/readyz", timeout=2.0)
        except httpx.HTTPError as exc:
            return SourceHealth(ok=False, detail=str(exc))
        return SourceHealth(
            ok=response.status_code == 200,
            detail="" if response.status_code == 200 else f"HTTP {response.status_code}",
        )
