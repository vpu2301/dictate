"""The frozen EvidenceSource protocol + registry with isolation (spec §4).

FROZEN as of EVA-S03: changing the protocol requires an ADR and an update to
the freeze-fixture suite (tests/unit/test_connector_freeze.py). Later source
kinds (web S04, pubmed/registry S09, drug S10) implement THIS interface.

Isolation policy: every connector runs under its own asyncio timeout
(descriptor.timeout_ms); exceptions and timeouts become
connector_meta.status='unavailable' — they never propagate, never 500.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from evidence_models import ConnectorDescriptor, ConnectorMeta, ConnectorStatus, EvidencePassage
from evidence_retrieval.domain.plan import Filters
from evidence_retrieval.domain.preprocess import PreparedQuery

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceHealth:
    ok: bool
    detail: str = ""


class SourceUnavailableError(Exception):
    """A connector's honest 'I cannot serve this' (stubs raise it always)."""


@runtime_checkable
class EvidenceSource(Protocol):
    descriptor: ConnectorDescriptor

    async def search(self, q: PreparedQuery, k: int, filters: Filters) -> list[EvidencePassage]: ...

    async def fetch_passage(self, passage_ref: str) -> EvidencePassage: ...

    async def health(self) -> SourceHealth: ...

    async def search_many(
        self, sub_queries: list[PreparedQuery], k: int, filters: Filters
    ) -> list[list[EvidencePassage]]: ...


class SearchManyDefault:
    """Default sequential search_many (S09 batch consumer); connectors
    override when they can batch natively."""

    async def search_many(
        self: EvidenceSource, sub_queries: list[PreparedQuery], k: int, filters: Filters
    ) -> list[list[EvidencePassage]]:
        return [await self.search(q, k, filters) for q in sub_queries]


class ConnectorRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, EvidenceSource] = {}

    def register(self, source: EvidenceSource) -> None:
        self._sources[source.descriptor.kind.value] = source

    def get(self, kind: str) -> EvidenceSource | None:
        return self._sources.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._sources)

    async def search_all(
        self, kinds: list[str], q: PreparedQuery, k: int, filters: Filters
    ) -> tuple[dict[str, list[EvidencePassage]], list[ConnectorMeta]]:
        """Run the requested connectors concurrently, isolated per §4."""

        async def one(kind: str) -> tuple[str, list[EvidencePassage], ConnectorMeta]:
            source = self._sources.get(kind)
            started = time.monotonic()
            if source is None:
                raise KeyError(kind)  # caller validated; defensive
            descriptor = source.descriptor
            try:
                passages = await asyncio.wait_for(
                    source.search(q, k, filters),
                    timeout=descriptor.timeout_ms / 1000,
                )
                status = ConnectorStatus.ok
            except SourceUnavailableError as exc:
                logger.info("connector.unavailable", extra={"kind": kind, "reason": str(exc)})
                passages, status = [], ConnectorStatus.unavailable
            except TimeoutError:
                logger.warning("connector.timeout", extra={"kind": kind})
                passages, status = [], ConnectorStatus.unavailable
            except Exception as exc:  # noqa: BLE001 — isolation: never propagate
                logger.exception("connector.error", exc_info=exc)
                passages, status = [], ConnectorStatus.unavailable
            meta = ConnectorMeta(
                kind=descriptor.kind,
                connector_id=descriptor.id,
                status=status,
                latency_ms=int((time.monotonic() - started) * 1000),
                count=len(passages),
            )
            return kind, passages, meta

        results = await asyncio.gather(*(one(kind) for kind in kinds))
        by_kind = {kind: passages for kind, passages, _ in results}
        metas = [meta for _, _, meta in results]
        return by_kind, metas
