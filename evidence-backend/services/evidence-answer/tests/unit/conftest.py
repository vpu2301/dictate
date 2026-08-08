"""Fakes for the pipeline suites.

Deliberately hand-written rather than `unittest.mock`: the pipeline's contract
with the gateway is "an async token stream", and a fake that actually streams
catches ordering bugs a MagicMock cannot. Rule P4 forbids mock data in shipped
code, not in tests — none of this is importable from the service package.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest

from evidence_models import (
    ConnectorKind,
    ConnectorMeta,
    ConnectorStatus,
    EvidencePassage,
    RetrieveResponse,
    WebSourceRef,
    WebTrustTier,
)
from models import GatewayError, GenerationResult

TENANT = UUID("11111111-1111-1111-1111-111111111111")


@dataclass
class FakeGateway:
    """Scripted `generate` / `generate_stream`.

    `generate` answers by role (triage, intent); `stream_script` is the
    synthesis output, one entry per attempt so a retry can be scripted to
    succeed after a structural failure.
    """

    generate_by_role: dict[str, str] = field(default_factory=dict)
    stream_script: list[str] = field(default_factory=list)
    fail_stream_with: Exception | None = None
    prompts_seen: list[tuple[str, str]] = field(default_factory=list)
    stream_calls: int = 0

    async def generate(
        self,
        role: str,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        stop: list[str] | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> GenerationResult:
        self.prompts_seen.append((role, prompt))
        if role not in self.generate_by_role:
            raise GatewayError(503, "gateway_unavailable", f"no script for {role}")
        return GenerationResult(
            text=self.generate_by_role[role], model_id=f"fake/{role}", temperature=temperature
        )

    async def generate_stream(
        self,
        role: str,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        stop: list[str] | None = None,
    ) -> AsyncIterator[str]:
        self.prompts_seen.append((role, prompt))
        index = self.stream_calls
        self.stream_calls += 1
        if self.fail_stream_with is not None:
            raise self.fail_stream_with
        if index >= len(self.stream_script):
            raise GatewayError(503, "gateway_unavailable", "stream script exhausted")
        # Chunked mid-line on purpose: a parser that only works on whole-line
        # chunks would pass a naive fake and fail against a real model.
        text = self.stream_script[index]
        for start in range(0, len(text), 7):
            yield text[start : start + 7]

    async def ready(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@dataclass
class FakeRetrieval:
    corpus_passages: list[EvidencePassage] = field(default_factory=list)
    web_passages: list[EvidencePassage] = field(default_factory=list)
    corpus_error: Exception | None = None
    web_error: Exception | None = None
    degraded: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def retrieve(
        self,
        *,
        query: str,
        sources: Any,
        k: int,
        tenant_id: UUID,
        timeout_s: float,
        locale: str | None = None,
        snapshot_id: UUID | None = None,
        intent: Any = None,
    ) -> RetrieveResponse:
        wanted = list(sources)
        self.calls.append({"sources": wanted, "query": query, "intent": intent, "k": k})
        if "web" in wanted:
            if self.web_error is not None:
                raise self.web_error
            return RetrieveResponse(
                passages=self.web_passages,
                connector_meta=[
                    ConnectorMeta(
                        kind=ConnectorKind.web,
                        connector_id="web@evidence-websearch",
                        status=ConnectorStatus.ok,
                        latency_ms=900,
                        count=len(self.web_passages),
                    )
                ],
            )
        if self.corpus_error is not None:
            raise self.corpus_error
        return RetrieveResponse(
            passages=self.corpus_passages,
            connector_meta=[
                ConnectorMeta(
                    kind=ConnectorKind.local_corpus,
                    connector_id="corpus@local",
                    status=ConnectorStatus.ok,
                    latency_ms=40,
                    count=len(self.corpus_passages),
                )
            ],
            degraded=self.degraded,
        )

    async def ready(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def corpus_passage(idx: int, text: str) -> EvidencePassage:
    return EvidencePassage(
        id=f"chunk-{idx}",
        connector_id="corpus@local",
        text=text,
        section_path=f"Guideline §{idx}",
        source_kind=ConnectorKind.local_corpus,
    )


def web_passage(idx: int, text: str, domain: str = "who.int") -> EvidencePassage:
    from datetime import UTC, datetime

    return EvidencePassage(
        id=f"web:{idx}",
        connector_id="web@evidence-websearch",
        text=text,
        section_path="WHO guidance",
        source_kind=ConnectorKind.web,
        web_ref=WebSourceRef(
            url=f"https://{domain}/page-{idx}",
            domain=domain,
            trust_tier=WebTrustTier.international_organization,
            accessed_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
            snapshot_ref=f"web/snapshot/{idx}",
        ),
    )


@pytest.fixture
def intent_json() -> str:
    return (
        '{"question_type": "therapy", '
        '"concepts": ["community acquired pneumonia", "amoxicillin"], '
        '"population": "adults", "negations": []}'
    )
