"""ConnectorRegistry.search_all isolation (spec §4/§9.6): timeouts and exceptions
become connector_meta status='unavailable' and never propagate."""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from evidence_retrieval.domain.plan import Filters
from evidence_retrieval.domain.preprocess import PreparedQuery, prepare
from evidence_retrieval.domain.registry import (
    ConnectorRegistry,
    SourceHealth,
    SourceUnavailableError,
)

from evidence_models import (
    ConnectorDescriptor,
    ConnectorKind,
    ConnectorStatus,
    EvidencePassage,
)


def _passage(connector_id: str) -> EvidencePassage:
    return EvidencePassage(
        id=f"chunk:{uuid4()}", connector_id=connector_id, text="text", chunk_id=uuid4()
    )


class FakeSource:
    def __init__(
        self,
        kind: ConnectorKind,
        *,
        passages: list[EvidencePassage] | None = None,
        delay_s: float = 0.0,
        exc: Exception | None = None,
        timeout_ms: int = 600,
    ) -> None:
        self.descriptor = ConnectorDescriptor(
            id=f"{kind.value}@test",
            kind=kind,
            display_name=kind.value,
            version="1.0",
            timeout_ms=timeout_ms,
        )
        self._passages = passages or []
        self._delay_s = delay_s
        self._exc = exc

    async def search(self, q: PreparedQuery, k: int, filters: Filters) -> list[EvidencePassage]:
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._exc is not None:
            raise self._exc
        return self._passages

    async def fetch_passage(self, passage_ref: str) -> EvidencePassage:
        raise SourceUnavailableError("not under test")

    async def health(self) -> SourceHealth:
        return SourceHealth(ok=True)

    async def search_many(
        self, sub_queries: list[PreparedQuery], k: int, filters: Filters
    ) -> list[list[EvidencePassage]]:
        return [await self.search(q, k, filters) for q in sub_queries]


def _registry(*sources: FakeSource) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    for source in sources:
        registry.register(source)
    return registry


async def test_slow_connector_times_out_without_stalling_the_others() -> None:
    """THE CHAOS TEST (spec §9.6): a hung connector costs its own timeout budget,
    not the wall clock of the whole retrieval."""
    healthy_passages = [_passage("tenant_corpus@test")]
    slow = FakeSource(ConnectorKind.local_corpus, delay_s=5.0, timeout_ms=100)
    healthy = FakeSource(ConnectorKind.tenant_corpus, passages=healthy_passages)
    registry = _registry(slow, healthy)

    started = time.monotonic()
    by_kind, metas = await registry.search_all(
        ["local_corpus", "tenant_corpus"], prepare("q"), 8, Filters()
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    meta_by_kind = {m.kind.value: m for m in metas}
    assert meta_by_kind["local_corpus"].status is ConnectorStatus.unavailable
    assert meta_by_kind["local_corpus"].count == 0
    assert by_kind["local_corpus"] == []
    assert meta_by_kind["tenant_corpus"].status is ConnectorStatus.ok
    assert meta_by_kind["tenant_corpus"].count == 1
    assert by_kind["tenant_corpus"] == healthy_passages


async def test_raising_connector_is_isolated_not_propagated() -> None:
    boom = FakeSource(ConnectorKind.local_corpus, exc=RuntimeError("boom"))
    healthy = FakeSource(ConnectorKind.tenant_corpus, passages=[_passage("tenant_corpus@test")])
    registry = _registry(boom, healthy)

    by_kind, metas = await registry.search_all(
        ["local_corpus", "tenant_corpus"], prepare("q"), 8, Filters()
    )

    meta_by_kind = {m.kind.value: m for m in metas}
    assert meta_by_kind["local_corpus"].status is ConnectorStatus.unavailable
    assert by_kind["local_corpus"] == []
    assert meta_by_kind["tenant_corpus"].status is ConnectorStatus.ok


async def test_source_unavailable_error_maps_to_unavailable_count_zero() -> None:
    honest = FakeSource(ConnectorKind.web, exc=SourceUnavailableError("planned"))
    registry = _registry(honest)

    by_kind, metas = await registry.search_all(["web"], prepare("q"), 8, Filters())

    (meta,) = metas
    assert meta.status is ConnectorStatus.unavailable
    assert meta.count == 0
    assert by_kind["web"] == []


async def test_latency_ms_recorded_non_negative() -> None:
    fast = FakeSource(ConnectorKind.local_corpus, passages=[_passage("local_corpus@test")])
    slow = FakeSource(ConnectorKind.web, delay_s=5.0, timeout_ms=50)
    registry = _registry(fast, slow)

    _, metas = await registry.search_all(["local_corpus", "web"], prepare("q"), 8, Filters())

    assert all(m.latency_ms >= 0 for m in metas)
    meta_by_kind = {m.kind.value: m for m in metas}
    # The timed-out connector burned at least its timeout budget.
    assert meta_by_kind["web"].latency_ms >= 50


async def test_all_requested_kinds_present_in_metas() -> None:
    kinds = ["local_corpus", "tenant_corpus", "web"]
    registry = _registry(
        FakeSource(ConnectorKind.local_corpus),
        FakeSource(ConnectorKind.tenant_corpus),
        FakeSource(ConnectorKind.web, exc=RuntimeError("down")),
    )

    by_kind, metas = await registry.search_all(kinds, prepare("q"), 8, Filters())

    assert {m.kind.value for m in metas} == set(kinds)
    assert set(by_kind) == set(kinds)
