"""QS1 layer 3: the runtime taint harness.

Layers 1 and 2 are static — an import contract and a type wall. They prove
that today's code *cannot* name a snapshot type. This harness proves the
stronger, dynamic property: **no canary token planted in patient-shaped data
ever appears in an outbound request**, whatever route the code takes to get
there.

It matters most for the sprint that does not exist yet. S04 has no patient
mode, so today the harness passes trivially on the web path and its real
value is the plumbing: a fake snapshot carrying canaries, an outbound
recorder that sees every byte the fetcher and metasearch client would send,
and an assertion that scans URLs, headers and bodies. From S05 on — when a
`PatientSnapshot` genuinely flows through the pipeline — this is the test
that fails if someone wires it to the web builder.

Wired as a permanent gate: `make qs1-taint` (and inside `make ci-with-db`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from evidence_websearch.domain.allowlist import Allowlist, DomainRule
from evidence_websearch.domain.fetcher import PageFetcher
from evidence_websearch.domain.metasearch import MetaSearchClient, build_query

from evidence_models import ClinicalConcept, ClinicalIntent, QuestionType, WebTrustTier

# Distinctive, unmistakable, and shaped like the things that actually leak.
CANARIES: dict[str, str] = {
    "patient_name": "ZQXCANARYPATIENTNAME",
    "ipn": "19850613CANARY",
    "mrn": "MRN-CANARY-99881",
    "phone": "+380671234567",
    "email": "canary.patient@example.org",
    "address": "вул. Канарейкова 7, Київ",
    "dob": "13.06.1985",
}


@dataclass
class FakeSnapshot:
    """Stands in for `PatientSnapshot` — which this test module deliberately
    does NOT import, because importing it here would be the very coupling the
    gate exists to forbid (`make check-qs1` scans the web path, and a test
    that models the leak must not create one).

    Every field is a canary, so any path that reads any field and forwards it
    is caught.
    """

    name: str = CANARIES["patient_name"]
    national_id: str = CANARIES["ipn"]
    mrn: str = CANARIES["mrn"]
    phone: str = CANARIES["phone"]
    email: str = CANARIES["email"]
    address: str = CANARIES["address"]
    date_of_birth: str = CANARIES["dob"]

    def as_free_text(self) -> str:
        return (
            f"{self.name}, {self.date_of_birth}, МРН {self.mrn}, ІПН "
            f"{self.national_id}, {self.phone}, {self.email}, {self.address}"
        )


@dataclass
class OutboundRecorder:
    """Records every request an httpx client would send."""

    requests: list[httpx.Request] = field(default_factory=list)

    def transport(self, *, status: int = 200, body: bytes = b"<html></html>") -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            # Read the body eagerly: a streaming request would otherwise be
            # consumed before the assertion sees it.
            request.read()
            self.requests.append(request)
            return httpx.Response(
                status,
                content=body,
                headers={"content-type": "text/html"},
                request=request,
            )

        return httpx.MockTransport(handler)

    def wire_bytes(self) -> str:
        """Everything that would have crossed the network, as one string."""
        parts: list[str] = []
        for request in self.requests:
            parts.append(str(request.url))
            parts.extend(f"{k}: {v}" for k, v in request.headers.items())
            parts.append(request.content.decode("utf-8", errors="replace"))
        return "\n".join(parts)


def assert_no_canaries(recorder: OutboundRecorder) -> None:
    wire = recorder.wire_bytes()
    leaked = sorted(name for name, value in CANARIES.items() if value in wire)
    assert not leaked, (
        f"QS1 VIOLATION — canary values reached the network: {leaked}\n"
        f"outbound traffic was:\n{wire}"
    )


ALLOWLIST = Allowlist(
    rules={
        "who.int": DomainRule(
            domain="who.int",
            trust_tier=WebTrustTier.international_organization,
            status="enabled",
            is_default=True,
            metadata_only=False,
        )
    }
)


def test_build_query_only_accepts_concepts_type_wall() -> None:
    """Layer 2, asserted at runtime: `build_query` takes a ClinicalIntent.

    A `FakeSnapshot` is not one, and pydantic-free duck typing does not save
    it — the function reads `.concepts`, which a snapshot does not have.
    """
    with pytest.raises(AttributeError):
        build_query(FakeSnapshot())  # type: ignore[arg-type]


def test_snapshot_free_text_never_becomes_a_web_query() -> None:
    """The realistic S05 mistake: someone builds an intent out of a snapshot's
    free text. The sanitizers must strip the identifier-shaped parts, and the
    canaries must not survive into the query."""
    snapshot = FakeSnapshot()
    # Deliberately hostile: a concept list built straight from patient text.
    tainted = ClinicalIntent(
        question_type=QuestionType.therapy,
        concepts=[
            ClinicalConcept(text=snapshot.national_id),
            ClinicalConcept(text=snapshot.phone),
            ClinicalConcept(text=snapshot.email),
            ClinicalConcept(text=snapshot.date_of_birth),
            ClinicalConcept(text="community acquired pneumonia"),
        ],
        population=snapshot.mrn,
    )

    query = build_query(tainted)

    assert "community acquired pneumonia" in query
    for name, value in CANARIES.items():
        if name in ("patient_name", "address"):
            # Not identifier-shaped: the sanitizer cannot catch a name that
            # looks like a word. That is exactly why the type wall exists —
            # asserted in the test above, and why S05's de-id stage is a
            # prerequisite for patient-aware web search.
            continue
        assert value not in query, f"{name} survived into the web query"


async def test_metasearch_request_carries_no_canaries() -> None:
    recorder = OutboundRecorder()
    client = MetaSearchClient(base_url="http://searxng.internal", timeout_s=2.0, results=5)
    client._http = httpx.AsyncClient(  # noqa: SLF001 — harness reaches in on purpose
        base_url="http://searxng.internal",
        transport=recorder.transport(body=json.dumps({"results": []}).encode()),
    )
    snapshot = FakeSnapshot()
    intent = ClinicalIntent(
        question_type=QuestionType.therapy,
        concepts=[ClinicalConcept(text="pneumonia"), ClinicalConcept(text=snapshot.mrn)],
    )

    await client.search(build_query(intent))

    assert recorder.requests, "the harness recorded no outbound request"
    assert_no_canaries(recorder)
    await client.aclose()


async def test_fetch_request_carries_no_canaries() -> None:
    recorder = OutboundRecorder()
    fetcher = PageFetcher(
        client=httpx.AsyncClient(transport=recorder.transport()),
        allow_http=True,
        timeout_s=2.0,
        max_bytes=1024,
        max_redirects=1,
        robots_timeout_s=1.0,
        user_agent="evidentia-test/1.0",
        resolve_dns=False,
    )

    result = await fetcher.fetch("https://who.int/guideline", allowlist=ALLOWLIST)

    assert result.status.value in ("ok", "garbage", "error")
    assert recorder.requests, "the harness recorded no outbound request"
    assert_no_canaries(recorder)


async def test_harness_detects_a_planted_leak() -> None:
    """Negative control (rule T1: prove the gate fails on a planted violation).

    If this passes silently, the harness is not actually looking at anything.
    """
    recorder = OutboundRecorder()
    client = httpx.AsyncClient(
        base_url="http://searxng.internal",
        transport=recorder.transport(body=b"{}"),
    )

    await client.get("/search", params={"q": CANARIES["ipn"]})

    with pytest.raises(AssertionError, match="QS1 VIOLATION"):
        assert_no_canaries(recorder)
    await client.aclose()
