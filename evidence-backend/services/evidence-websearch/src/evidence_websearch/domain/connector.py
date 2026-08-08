"""The `web` evidence source (S03 `EvidenceSource`, kind='web'), server side.

Rule E2 forbids one service importing another, so this module does not import
`evidence_retrieval`'s Protocol — it implements its *shape*: given a prepared
query it returns `EvidencePassage[]` with `source_kind='web'` and a
`WebSourceRef` attached. `evidence-retrieval` registers a thin HTTP adapter
that satisfies the frozen protocol and calls `POST /web/search` here, which is
why the S03 freeze fixtures pass unchanged.

One query, in order:

    metasearch(concepts) → candidate URLs
      → allowlist filter (off-list results are reported, never fetched)
      → cache lookup (a fresh page is reused; nothing leaves the network)
      → proxy fetch → extract → injection screen → snapshot + row
      → chunk → embed → ephemeral index
      → similarity search → passages

Every drop is a `PageOutcome` with a reason, and the reasons ride back to the
caller so they land in the answer trace (spec §11: "never garbage in").
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
from db import tenant_connection

from evidence_models import (
    ClinicalIntent,
    ConnectorKind,
    EvidencePassage,
    PassageScores,
    WebSourceRef,
)
from evidence_safety import screen_text
from evidence_websearch.adapters import pg
from evidence_websearch.config import Settings
from evidence_websearch.domain import allowlist as allowlist_domain
from evidence_websearch.domain.allowlist import Allowlist, DomainRule
from evidence_websearch.domain.cache import PageCache, url_hash
from evidence_websearch.domain.ephemeral import EphemeralChunk, EphemeralIndex, chunk_text
from evidence_websearch.domain.extract import extract
from evidence_websearch.domain.fetcher import FetchStatus, PageFetcher
from evidence_websearch.domain.metasearch import MetaSearchClient, MetaSearchError, build_query

logger = logging.getLogger(__name__)

CONNECTOR_ID = "web@searxng-1"


@dataclass(frozen=True, slots=True)
class PageOutcome:
    url: str
    domain: str
    status: str
    reason: str = ""
    from_cache: bool = False


@dataclass(slots=True)
class WebSearchOutcome:
    passages: list[EvidencePassage] = field(default_factory=list)
    pages: list[PageOutcome] = field(default_factory=list)
    # Results the metasearch found on domains nobody has allowlisted. Surfaced
    # as "found, not fetched" so an admin can request an addition (spec §edge).
    not_allowlisted: list[str] = field(default_factory=list)
    degraded: bool = False
    degrade_reason: str = ""
    indexed_chunks: int = 0
    index_bytes: int = 0

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for page in self.pages:
            tally[page.status] = tally.get(page.status, 0) + 1
        return tally


@dataclass(slots=True)
class WebSearchRuntime:
    settings: Settings
    pool: asyncpg.Pool
    metasearch: MetaSearchClient
    fetcher: PageFetcher
    cache: PageCache
    index: EphemeralIndex


class WebConnector:
    """Not a FastAPI concern: `routers/search.py` is a thin shell over this."""

    def __init__(self, runtime: WebSearchRuntime) -> None:
        self._rt = runtime

    async def search(
        self, *, intent: ClinicalIntent, tenant_id: UUID, k: int, locale: str | None = None
    ) -> WebSearchOutcome:
        settings = self._rt.settings
        outcome = WebSearchOutcome()

        # QS1: the query is built from de-identified concepts and nothing else.
        query = build_query(intent)
        if not query:
            outcome.degraded = True
            outcome.degrade_reason = "empty_intent"
            return outcome

        try:
            hits = await self._rt.metasearch.search(query, language=locale)
        except MetaSearchError as exc:
            logger.warning("websearch.metasearch_unavailable", extra={"reason": str(exc)})
            outcome.degraded = True
            outcome.degrade_reason = "metasearch_unavailable"
            return outcome

        async with tenant_connection(self._rt.pool, tenant_id) as conn:
            allowlist = await allowlist_domain.load_for_tenant(conn, tenant_id=tenant_id)

        candidates: list[tuple[str, DomainRule]] = []
        seen_hashes: set[str] = set()
        for hit in hits:
            rule = allowlist.match(_host_of(hit.url))
            if rule is None:
                outcome.not_allowlisted.append(hit.url)
                continue
            digest = url_hash(hit.url)
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            candidates.append((hit.url, rule))
            if len(candidates) >= settings.fetch_max_pages:
                break

        semaphore = asyncio.Semaphore(settings.fetch_concurrency)

        async def one(url: str, rule: DomainRule) -> tuple[PageOutcome, list[EphemeralChunk]]:
            async with semaphore:
                return await self._page_chunks(
                    url=url, rule=rule, tenant_id=tenant_id, allowlist=allowlist
                )

        results = await asyncio.gather(
            *(one(url, rule) for url, rule in candidates), return_exceptions=True
        )
        chunks: list[EphemeralChunk] = []
        for (url, _rule), result in zip(candidates, results, strict=True):
            if isinstance(result, BaseException):
                # Connector isolation: one bad page never sinks the query.
                logger.warning(
                    "websearch.page_failed",
                    extra={"error": type(result).__name__},
                )
                outcome.pages.append(
                    PageOutcome(
                        url=url,
                        domain=_host_of(url),
                        status="error",
                        reason=f"exception:{type(result).__name__}",
                    )
                )
                continue
            page_outcome, page_chunks = result
            outcome.pages.append(page_outcome)
            chunks.extend(page_chunks)

        if not chunks:
            if not outcome.pages:
                outcome.degraded = True
                outcome.degrade_reason = "no_allowlisted_results"
            return outcome

        key = EphemeralIndex.key(str(tenant_id), f"{query}|{tenant_id}")
        outcome.indexed_chunks = await self._rt.index.build(key=key, chunks=chunks)
        outcome.index_bytes = await self._rt.index.size_bytes(key)
        ranked = await self._rt.index.search(key=key, query=query, k=k)

        now = datetime.now(tz=UTC)
        for chunk, score in ranked:
            matched = allowlist.match(chunk.domain)
            if matched is None:  # pragma: no cover — chunks only exist for allowlisted pages
                continue
            outcome.passages.append(
                EvidencePassage(
                    id=chunk.id,
                    connector_id=CONNECTOR_ID,
                    text=chunk.text,
                    section_path=chunk.title or None,
                    source_kind=ConnectorKind.web,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    score=score,
                    scores=PassageScores(dense=score, final=score),
                    web_ref=WebSourceRef(
                        url=chunk.page_url,
                        domain=chunk.domain,
                        trust_tier=matched.trust_tier,
                        accessed_at=now,
                        snapshot_ref=PageCache.snapshot_key(url_hash(chunk.page_url)),
                    ),
                )
            )
        return outcome

    async def _page_chunks(
        self, *, url: str, rule: DomainRule, tenant_id: UUID, allowlist: Allowlist
    ) -> tuple[PageOutcome, list[EphemeralChunk]]:
        settings = self._rt.settings
        digest = url_hash(url)
        metadata_only = rule.metadata_only
        domain = rule.domain

        # 1. Cache first — a fresh page means no network call at all.
        async with tenant_connection(self._rt.pool, tenant_id) as conn:
            cached = await pg.get_page(conn, tenant_id=tenant_id, url_hash=digest)
        if cached is not None and _is_fresh(cached.fetched_at, settings.page_cache_ttl_s):
            if cached.status != "ok" or not cached.extract_ref:
                return (
                    PageOutcome(
                        url=url,
                        domain=cached.domain,
                        status=cached.status,
                        reason=cached.skip_reason or "cached_non_ok",
                        from_cache=True,
                    ),
                    [],
                )
            text = await self._rt.cache.get_extract(key=cached.extract_ref, tenant_id=tenant_id)
            return (
                PageOutcome(url=url, domain=cached.domain, status="ok", from_cache=True),
                self._chunk(text, url=cached.url, domain=cached.domain, title=cached.title or ""),
            )

        # 2. Metadata-only sources are cited, never fetched for body text.
        if metadata_only:
            async with tenant_connection(self._rt.pool, tenant_id) as conn:
                await pg.upsert_page(
                    conn,
                    tenant_id=tenant_id,
                    url_hash=digest,
                    url=url,
                    domain=domain,
                    title=None,
                    robots_ok=True,
                    license_flag="metadata_only",
                    status="paywalled",
                    http_status=None,
                    content_type=None,
                    byte_size=None,
                    snapshot_ref=None,
                    extract_ref=None,
                    skip_reason="metadata_only_source",
                )
            return (
                PageOutcome(
                    url=url, domain=domain, status="paywalled", reason="metadata_only_source"
                ),
                [],
            )

        # 3. Fetch through the proxy.
        result = await self._rt.fetcher.fetch(url, allowlist=allowlist)
        status = result.status
        skip_reason = result.skip_reason
        title: str | None = None
        snapshot_ref: str | None = None
        extract_ref: str | None = None
        chunks: list[EphemeralChunk] = []

        if result.usable:
            html = result.body.decode("utf-8", errors="replace")
            extraction = extract(
                html,
                min_chars=settings.extract_min_chars,
                max_link_density=settings.extract_max_link_density,
            )
            title = extraction.title or None
            if not extraction.ok:
                status = (
                    FetchStatus.paywalled
                    if extraction.skip_reason == "extract_paywall"
                    else FetchStatus.garbage
                )
                skip_reason = extraction.skip_reason
            elif screen_text(extraction.text):
                # LM4: a page carrying instruction-shaped payloads is dropped.
                # No quarantine queue for the open web — nobody curates it.
                status = FetchStatus.quarantined
                skip_reason = "injection_screen"
                logger.warning("websearch.injection_screened", extra={"domain": result.domain})
            else:
                snapshot_ref = await self._rt.cache.put_snapshot(
                    digest=digest, body=result.body, tenant_id=tenant_id
                )
                extract_ref = await self._rt.cache.put_extract(
                    digest=digest, text=extraction.text, tenant_id=tenant_id
                )
                chunks = self._chunk(
                    extraction.text,
                    url=result.final_url or url,
                    domain=result.domain,
                    title=extraction.title,
                )

        async with tenant_connection(self._rt.pool, tenant_id) as conn:
            await pg.upsert_page(
                conn,
                tenant_id=tenant_id,
                url_hash=digest,
                url=result.final_url or url,
                domain=result.domain or domain,
                title=title,
                robots_ok=result.robots_ok,
                license_flag="open" if status is FetchStatus.ok else "unknown",
                status=status.value,
                http_status=result.http_status,
                content_type=result.content_type,
                byte_size=len(result.body) or None,
                snapshot_ref=snapshot_ref,
                extract_ref=extract_ref,
                skip_reason=skip_reason or None,
            )
        return (
            PageOutcome(
                url=url,
                domain=result.domain or domain,
                status=status.value,
                reason=skip_reason,
            ),
            chunks,
        )

    def _chunk(self, text: str, *, url: str, domain: str, title: str) -> list[EphemeralChunk]:
        settings = self._rt.settings
        return chunk_text(
            text,
            page_url=url,
            domain=domain,
            title=title,
            size=settings.ephemeral_chunk_chars,
            overlap=settings.ephemeral_chunk_overlap,
            max_chunks=settings.ephemeral_max_chunks_per_page,
        )


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").casefold()


def _is_fresh(fetched_at: datetime, ttl_seconds: int) -> bool:
    return (datetime.now(tz=UTC) - fetched_at).total_seconds() < ttl_seconds
