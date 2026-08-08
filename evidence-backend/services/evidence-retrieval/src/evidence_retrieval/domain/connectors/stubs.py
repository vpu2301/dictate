"""Honest 'unavailable' stubs (rule P4: no fake data, ever) for source kinds
whose real connectors land later: pubmed + guideline_registry (S09), drug
(S10). They register so the registry/meta surface is complete; every search
raises SourceUnavailableError → connector_meta 'unavailable'.

`web` graduated in EVA-S04 (connectors/web.py) and is no longer stubbed.
"""

from __future__ import annotations

from evidence_models import ConnectorDescriptor, ConnectorKind, EvidencePassage
from evidence_retrieval.domain.plan import Filters
from evidence_retrieval.domain.preprocess import PreparedQuery
from evidence_retrieval.domain.registry import (
    SearchManyDefault,
    SourceHealth,
    SourceUnavailableError,
)

_PLANNED = {
    ConnectorKind.pubmed: "EVA-S09",
    ConnectorKind.guideline_registry: "EVA-S09",
    ConnectorKind.drug: "EVA-S10",
}


class UnavailableSource(SearchManyDefault):
    def __init__(self, kind: ConnectorKind) -> None:
        self.descriptor = ConnectorDescriptor(
            id=f"{kind.value}@planned",
            kind=kind,
            display_name=f"{kind.value} (not yet implemented — {_PLANNED[kind]})",
            version="0.0",
            enabled=False,
            needs_egress=kind is ConnectorKind.pubmed,
            timeout_ms=100,
        )

    async def search(self, q: PreparedQuery, k: int, filters: Filters) -> list[EvidencePassage]:
        raise SourceUnavailableError(
            f"connector {self.descriptor.kind.value} arrives in {_PLANNED[self.descriptor.kind]}"
        )

    async def fetch_passage(self, passage_ref: str) -> EvidencePassage:
        raise SourceUnavailableError("not implemented")

    async def health(self) -> SourceHealth:
        return SourceHealth(ok=False, detail="planned, not implemented")


def all_stubs() -> list[UnavailableSource]:
    return [UnavailableSource(kind) for kind in _PLANNED]
