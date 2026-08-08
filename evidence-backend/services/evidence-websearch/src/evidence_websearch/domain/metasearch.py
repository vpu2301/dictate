"""SearXNG metasearch client + the QS1 query builder.

**QS1 (the hard invariant of this sprint): patient data never enters a web
query.** This module is where that is made true structurally, not carefully:

    build_query(intent: ClinicalIntent) -> str

`ClinicalIntent` is de-identified by construction (its `concepts` hold
extracted clinical terms and codings, never free text). There is no other
entry point — `search()` takes a query string that only `build_query` can
legitimately produce, and this module imports no snapshot-bearing type. The
import contract in `pyproject.toml` (`[importlinter]` QS1 contract) makes
`evidence_websearch` importing `evidence_models.snapshot` a CI failure, so
the type wall cannot be quietly climbed over later.

Layer 3 is the runtime taint harness (`tests/qs1/`), which plants canary
tokens in a fake snapshot and asserts no outbound request ever carries one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from evidence_models import ClinicalIntent
from evidence_safety import identifier_shaped

logger = logging.getLogger(__name__)

# Hard cap on the query the metasearch engine ever sees. A long query is a
# smell (someone passed a narrative through), and truncation is not a defense
# we rely on — it is the last line after the type wall.
MAX_QUERY_CHARS = 300
MAX_CONCEPTS = 8

# Concept text that survives into a query: clinical terms only. Digits are
# allowed (e.g. "type 2 diabetes", "stage 4"), but identifier SHAPES — long
# digit runs, dates, contact details, record numbers — are dropped by the
# shared screen in `evidence_safety`. A ClinicalIntent carrying one of those
# is already a bug upstream; dropping it here means the bug cannot become a
# privacy incident.


@dataclass(frozen=True, slots=True)
class SearchHit:
    url: str
    title: str
    snippet: str
    engine: str


def _clean_concept(text: str) -> str | None:
    candidate = " ".join(text.strip().split())
    if not candidate or len(candidate) > 80:
        return None
    shape = identifier_shaped(candidate)
    if shape is not None:
        logger.warning(
            "qs1.identifier_shaped_concept_dropped",
            # The SHAPE, never the value: a log line is the last place a
            # leaked identifier should come to rest.
            extra={"shape": shape, "length": len(candidate)},
        )
        return None
    return candidate


def build_query(intent: ClinicalIntent) -> str:
    """Build the metasearch query from de-identified concepts ONLY (QS1).

    Deliberately narrow: `concepts[].text`, `population`, and the question
    type. Nothing else on `ClinicalIntent` is free text, and nothing outside
    `ClinicalIntent` is accepted at all.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for concept in intent.concepts[:MAX_CONCEPTS]:
        cleaned = _clean_concept(concept.text)
        if cleaned and cleaned.casefold() not in seen:
            terms.append(cleaned)
            seen.add(cleaned.casefold())
    if intent.population:
        cleaned = _clean_concept(intent.population)
        if cleaned and cleaned.casefold() not in seen:
            terms.append(cleaned)
    query = " ".join(terms)
    return query[:MAX_QUERY_CHARS].strip()


class MetaSearchError(Exception):
    """SearXNG unreachable or answering badly — callers degrade, never fail."""


class MetaSearchClient:
    """Self-hosted SearXNG (rule LM1/P7: no external search API, ever).

    Egress note: SearXNG itself talks to upstream engines through the same
    egress proxy (compose wiring); this hop is internal, so it does NOT go
    through the proxy.
    """

    def __init__(self, *, base_url: str, timeout_s: float, results: int) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self._results = results

    async def search(self, query: str, *, language: str | None = None) -> list[SearchHit]:
        if not query.strip():
            return []
        params: dict[str, str] = {"q": query, "format": "json", "safesearch": "0"}
        if language:
            params["language"] = language
        try:
            response = await self._http.get("/search", params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MetaSearchError(str(exc)) from exc
        hits: list[SearchHit] = []
        for raw in payload.get("results", [])[: self._results]:
            url = str(raw.get("url", "")).strip()
            if not url:
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=str(raw.get("title", "")).strip(),
                    snippet=str(raw.get("content", "") or "").strip(),
                    engine=str(raw.get("engine", "") or "unknown"),
                )
            )
        return hits

    async def ping(self) -> bool:
        try:
            response = await self._http.get("/healthz", timeout=2.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self._http.aclose()
