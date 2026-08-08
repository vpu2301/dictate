"""THE PROTOCOL FREEZE SUITE (spec §4).

EvidenceSource + ConnectorDescriptor + the EvidencePassage wire shape are FROZEN
as of EVA-S03. A failing test here means the connector contract changed — that
requires an ADR and a deliberate update of this suite plus the recorded fixture,
never a quiet edit.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from evidence_retrieval.domain.connectors.stubs import all_stubs
from evidence_retrieval.domain.plan import Filters
from evidence_retrieval.domain.preprocess import PreparedQuery
from evidence_retrieval.domain.registry import EvidenceSource, SourceHealth

from evidence_models import ConnectorDescriptor, ConnectorKind, EvidencePassage

FIXTURE = Path(__file__).parent.parent / "fixtures" / "connector_freeze_v1.json"

FROZEN_METHODS = {"search", "fetch_passage", "health", "search_many"}
FROZEN_DESCRIPTOR_FIELDS = {
    "id",
    "kind",
    "display_name",
    "version",
    "enabled",
    "authority_default",
    "needs_egress",
    "tenant_flaggable",
    "timeout_ms",
}


class MinimalCorpusConnector:
    """The smallest corpus-shaped object that must satisfy the protocol."""

    def __init__(self) -> None:
        self.descriptor = ConnectorDescriptor(
            id="local_corpus@1.0",
            kind=ConnectorKind.local_corpus,
            display_name="Local corpus",
            version="1.0",
        )

    async def search(self, q: PreparedQuery, k: int, filters: Filters) -> list[EvidencePassage]:
        return []

    async def fetch_passage(self, passage_ref: str) -> EvidencePassage:
        return EvidencePassage(
            id=passage_ref, connector_id=self.descriptor.id, text="", chunk_id=uuid4()
        )

    async def health(self) -> SourceHealth:
        return SourceHealth(ok=True)

    async def search_many(
        self, sub_queries: list[PreparedQuery], k: int, filters: Filters
    ) -> list[list[EvidencePassage]]:
        return [[] for _ in sub_queries]


def test_protocol_is_runtime_checkable_and_implementations_satisfy_it() -> None:
    # runtime_checkable Protocol: isinstance checks member PRESENCE, not signatures.
    assert getattr(EvidenceSource, "_is_runtime_protocol", False) is True
    assert isinstance(MinimalCorpusConnector(), EvidenceSource)
    for stub in all_stubs():
        assert isinstance(stub, EvidenceSource)
    # Negative control: an arbitrary object does not satisfy the protocol.
    assert not isinstance(object(), EvidenceSource)


def test_frozen_protocol_surface_methods() -> None:
    attrs = set(EvidenceSource.__protocol_attrs__)
    methods = {name for name in attrs if callable(getattr(EvidenceSource, name, None))}
    assert methods == FROZEN_METHODS
    # The descriptor data member is part of the frozen surface too.
    assert attrs == FROZEN_METHODS | {"descriptor"}


def test_frozen_connector_descriptor_fields() -> None:
    assert set(ConnectorDescriptor.model_fields) == FROZEN_DESCRIPTOR_FIELDS


def test_recorded_passage_round_trips_unchanged() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    recorded = fixture["response_passage"]
    passage = EvidencePassage.model_validate(recorded)
    assert passage.model_dump(mode="json", exclude_none=True) == recorded


def test_recorded_request_still_parses() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    request = fixture["request"]
    assert isinstance(request["query"], str) and request["query"]
    assert isinstance(request["k"], int)
    filters = Filters.model_validate(request["filters"])
    assert filters.model_dump(mode="json") == request["filters"]
