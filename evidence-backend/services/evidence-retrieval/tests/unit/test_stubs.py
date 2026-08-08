"""Honest 'unavailable' stubs (rule P4): no fake data, ever."""

from __future__ import annotations

import pytest
from evidence_retrieval.domain.connectors.stubs import all_stubs
from evidence_retrieval.domain.plan import Filters
from evidence_retrieval.domain.preprocess import prepare
from evidence_retrieval.domain.registry import ConnectorRegistry, SourceUnavailableError

from evidence_models import ConnectorKind, ConnectorStatus

# `web` left this set in EVA-S04 when the real connector landed
# (connectors/web.py); see test_web_connector.py.
EXPECTED_KINDS = {
    ConnectorKind.pubmed,
    ConnectorKind.guideline_registry,
    ConnectorKind.drug,
}


def test_all_stubs_covers_the_planned_kinds_all_disabled() -> None:
    stubs = all_stubs()

    assert {s.descriptor.kind for s in stubs} == EXPECTED_KINDS
    assert len(stubs) == 3
    assert all(s.descriptor.enabled is False for s in stubs)


async def test_every_stub_search_raises_source_unavailable() -> None:
    for stub in all_stubs():
        with pytest.raises(SourceUnavailableError):
            await stub.search(prepare("q"), 8, Filters())


async def test_every_stub_health_not_ok_and_fetch_raises() -> None:
    for stub in all_stubs():
        health = await stub.health()
        assert health.ok is False
        with pytest.raises(SourceUnavailableError):
            await stub.fetch_passage("chunk:whatever")


async def test_registry_reports_stubs_unavailable_with_zero_passages() -> None:
    """Honest emptiness: the meta surface is complete, but no fake passages leak."""
    registry = ConnectorRegistry()
    for stub in all_stubs():
        registry.register(stub)
    kinds = sorted(k.value for k in EXPECTED_KINDS)

    by_kind, metas = await registry.search_all(kinds, prepare("q"), 8, Filters())

    assert {m.kind.value for m in metas} == set(kinds)
    for meta in metas:
        assert meta.status is ConnectorStatus.unavailable
        assert meta.count == 0
    assert all(passages == [] for passages in by_kind.values())
