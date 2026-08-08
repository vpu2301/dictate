"""POST /web/search — internal, service-token authenticated.

The request body is the QS1 type wall at the API boundary: it accepts a
`ClinicalIntent` and a tenant id, and nothing else. There is no field on this
model through which a `PatientSnapshot`, a patient id, or raw question text
could arrive — a caller that wants the web searched must first have reduced
the question to de-identified concepts.

Identity does not attach here (S03 §7 precedent): `evidence-answer` carries
the user, this hop carries the tenant.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from evidence_models import ClinicalIntent, EvidencePassage
from evidence_websearch.config import settings
from evidence_websearch.domain.connector import CONNECTOR_ID, WebConnector
from evidence_websearch.main_deps import get_state

logger = logging.getLogger(__name__)
router = APIRouter(tags=["search"])


async def require_service_token(
    x_service_token: Annotated[str | None, Header()] = None,
) -> None:
    if settings.service_token is None:
        return  # dev-open; startup WARNING emitted (rule BE7)
    if x_service_token != settings.service_token:
        raise HTTPException(status_code=401, detail="invalid service token")


class WebSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # QS1: de-identified concepts only. Do NOT add a free-text field here.
    intent: ClinicalIntent
    tenant_id: UUID
    k: int = Field(default=8, ge=1, le=50)
    locale: str | None = None
    trace_id: str | None = None


class PageTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    domain: str
    status: str
    reason: str = ""
    from_cache: bool = False


class WebSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = CONNECTOR_ID
    passages: list[EvidencePassage] = []
    pages: list[PageTrace] = []
    # "Found, not fetched": good-looking sources nobody has allowlisted yet.
    not_allowlisted: list[str] = []
    degraded: bool = False
    degrade_reason: str = ""
    indexed_chunks: int = 0


@router.post("/web/search", dependencies=[Depends(require_service_token)])
async def web_search(body: WebSearchRequest) -> WebSearchResponse:
    state = get_state()
    connector = WebConnector(state.runtime)
    outcome = await connector.search(
        intent=body.intent, tenant_id=body.tenant_id, k=body.k, locale=body.locale
    )
    for status, count in sorted(outcome.counts().items()):
        state.metrics.fetch_outcomes[status] = state.metrics.fetch_outcomes.get(status, 0) + count
    state.metrics.index_bytes = outcome.index_bytes
    return WebSearchResponse(
        passages=outcome.passages,
        pages=[
            PageTrace(
                url=page.url,
                domain=page.domain,
                status=page.status,
                reason=page.reason,
                from_cache=page.from_cache,
            )
            for page in outcome.pages
        ],
        not_allowlisted=outcome.not_allowlisted,
        degraded=outcome.degraded,
        degrade_reason=outcome.degrade_reason,
        indexed_chunks=outcome.indexed_chunks,
    )
