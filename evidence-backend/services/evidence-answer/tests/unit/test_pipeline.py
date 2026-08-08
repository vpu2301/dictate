"""Golden streaming e2e (TC-1) and the failure paths around it.

These drive the real pipeline against fake upstreams, so what is asserted is
the thing the SPA actually consumes: event ORDER, segment kinds, and that
every citation resolves to a source in the same envelope.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from conftest import TENANT, FakeGateway, FakeRetrieval, corpus_passage, web_passage
from evidence_answer.adapters.retrieval import RetrievalUnavailableError
from evidence_answer.config import Settings
from evidence_answer.domain.pipeline import PipelineDeps, QuickSearchPipeline

from evidence_models import AnswerStatus, SegmentKind, StageOutcome

GOOD_SYNTHESIS = (
    "[SUMMARY|evidence] Amoxicillin is first line for mild CAP in adults [S1].\n"
    "[SUMMARY|uncertainty] Guidance differs for patients with penicillin allergy [S2].\n"
    "[DETAIL|evidence] Typical duration is five days in responders [S2].\n"
    "[DETAIL|missing_info] Renal impairment dosing is not covered by these sources.\n"
    "[DETAIL|next_step] Check local resistance data before prescribing.\n"
)

DANGLING_SYNTHESIS = "[SUMMARY|evidence] A confident claim [S9].\n"
UNCITED_SYNTHESIS = "[SUMMARY|evidence] A confident claim with no source at all.\n"


def settings(**overrides: Any) -> Settings:
    base = {
        "TESTING": "true",
        "EVA_ANSWER_WEB_ENABLED": "true",
        "EVA_ANSWER_TRIAGE_CLASSIFIER": "false",
    }
    base.update({k: str(v) for k, v in overrides.items()})
    return Settings(**base)  # type: ignore[arg-type]


class Recorder:
    """Collects SSE frames and decodes them back into events."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    async def __call__(self, frame: bytes) -> None:
        self.frames.append(frame)

    @property
    def events(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for frame in self.frames:
            text = frame.decode()
            if text.startswith(":"):  # heartbeat comment
                continue
            data_line = next(line for line in text.splitlines() if line.startswith("data: "))
            out.append(json.loads(data_line[6:]))
        return out

    @property
    def kinds(self) -> list[str]:
        return [event["event"] for event in self.events]


async def run(
    *,
    gateway: FakeGateway,
    retrieval: FakeRetrieval,
    question: str = "Empiric therapy for CAP in adults?",
    locale: str = "en",
    config: Settings | None = None,
) -> tuple[Any, Recorder]:
    recorder = Recorder()
    pipeline = QuickSearchPipeline(
        PipelineDeps(
            settings=config or settings(),
            gateway=gateway,  # type: ignore[arg-type]
            retrieval=retrieval,  # type: ignore[arg-type]
        )
    )
    result = await pipeline.run(
        question=question,
        locale=locale,
        tenant_id=TENANT,
        question_id=uuid4(),
        answer_id=uuid4(),
        emit=recorder,
    )
    return result, recorder


@pytest.fixture
def gateway(intent_json: str) -> FakeGateway:
    return FakeGateway(
        generate_by_role={"generator.fast": intent_json},
        stream_script=[GOOD_SYNTHESIS],
    )


@pytest.fixture
def retrieval() -> FakeRetrieval:
    return FakeRetrieval(
        corpus_passages=[
            corpus_passage(1, "Amoxicillin is recommended as first-line therapy."),
            corpus_passage(2, "Treat for five days; reassess at 48-72 hours."),
        ],
        web_passages=[web_passage(1, "WHO recommends severity-guided therapy.")],
    )


# ── TC-1: golden streaming e2e ──────────────────────────────────────────


async def test_golden_stream_event_order(gateway: FakeGateway, retrieval: FakeRetrieval) -> None:
    _, recorder = await run(gateway=gateway, retrieval=retrieval)

    kinds = recorder.kinds
    assert kinds[0] == "header"
    assert kinds[-1] == "done"
    # header → summary* → detail* → source* → late_source* → done
    order = ["header", "summary_segment", "detail_segment", "source", "late_source", "done"]
    positions = [order.index(kind) for kind in kinds]
    assert positions == sorted(positions), kinds


async def test_golden_stream_segments_and_citations_resolve(
    gateway: FakeGateway, retrieval: FakeRetrieval
) -> None:
    result, recorder = await run(gateway=gateway, retrieval=retrieval)
    envelope = result.envelope

    assert envelope.status is AnswerStatus.ok
    assert [s.kind for s in envelope.summary_segments] == [
        SegmentKind.evidence,
        SegmentKind.uncertainty,
    ]
    assert [s.kind for s in envelope.detail_segments] == [
        SegmentKind.evidence,
        SegmentKind.missing_info,
        SegmentKind.next_step,
    ]
    # The envelope validator already enforces this, but assert it explicitly:
    # it is the property the whole sprint exists to deliver.
    source_ids = {s.id for s in envelope.sources}
    for segment in [*envelope.summary_segments, *envelope.detail_segments]:
        for citation in segment.citations:
            assert citation.source_id in source_ids
    assert recorder.kinds.count("summary_segment") == 2
    assert recorder.kinds.count("detail_segment") == 3


async def test_header_announces_late_web_sources_before_any_content(
    gateway: FakeGateway, retrieval: FakeRetrieval
) -> None:
    _, recorder = await run(gateway=gateway, retrieval=retrieval)

    header = recorder.events[0]["header"]
    assert header["late_sources_expected"] is True
    assert header["verified"] is False  # until S06
    assert header["mode"] == "quick_search"


async def test_web_passages_arrive_as_late_sources(
    gateway: FakeGateway, retrieval: FakeRetrieval
) -> None:
    result, recorder = await run(gateway=gateway, retrieval=retrieval)

    late = [e["source"] for e in recorder.events if e["event"] == "late_source"]
    assert len(late) == 1
    assert late[0]["kind"] == "web"
    assert late[0]["web"]["domain"] == "who.int"
    assert late[0]["web"]["snapshot_ref"] == "web/snapshot/1"
    assert "web@evidence-websearch" in result.connectors


async def test_provenance_records_connectors_pins_and_prompt_versions(
    gateway: FakeGateway, retrieval: FakeRetrieval
) -> None:
    result, _ = await run(gateway=gateway, retrieval=retrieval)
    provenance = result.provenance

    assert "corpus@local" in provenance.connectors
    assert "web@evidence-websearch" in provenance.connectors
    assert set(provenance.prompt_versions) == {"triage", "intent", "synthesis"}
    assert provenance.pipeline_version == "quick-search-1.0"
    assert provenance.passage_ids  # the evidence actually used
    assert provenance.web_refs and provenance.web_refs[0].domain == "who.int"
    # No patient context in quick mode.
    assert provenance.snapshot_hash is None
    assert provenance.consumed_fields == []


async def test_every_stage_is_traced(gateway: FakeGateway, retrieval: FakeRetrieval) -> None:
    result, _ = await run(gateway=gateway, retrieval=retrieval)

    stages = [t.stage for t in result.traces]
    assert stages == ["triage", "intent", "plan", "retrieve_corpus", "synthesize", "retrieve_web"]
    assert all(t.ended_at >= t.started_at for t in result.traces)
    assert all(t.outcome is StageOutcome.ok for t in result.traces)


# ── TC-3: deflection ────────────────────────────────────────────────────


async def test_emergency_question_is_deflected_with_safe_messaging(
    gateway: FakeGateway, retrieval: FakeRetrieval
) -> None:
    result, recorder = await run(
        gateway=gateway,
        retrieval=retrieval,
        question="Patient is coding right now, what do I do?",
    )

    assert result.deflected
    assert result.deflection_reason == "emergency"
    assert result.envelope.status is AnswerStatus.deflected
    done = recorder.events[-1]["done"]
    assert done["status"] == "deflected"
    assert done["triage"]["reason_code"] == "emergency"
    assert "emergency services" in done["triage"]["message"]
    # A deflection must not consult retrieval or the synthesis model at all.
    assert retrieval.calls == []
    assert gateway.stream_calls == 0
    # Critical severity + acknowledgment gating (rule CS2).
    flag = result.envelope.flags[0]
    assert flag.severity.value == "critical"
    assert flag.requires_ack is True


async def test_ukrainian_deflection_is_localized(
    gateway: FakeGateway, retrieval: FakeRetrieval
) -> None:
    result, _ = await run(
        gateway=gateway,
        retrieval=retrieval,
        question="Пацієнт непритомний, немає пульсу, що робити зараз?",
        locale="uk",
    )

    assert result.deflected
    assert "103" in result.envelope.summary_segments[0].text


# ── TC-2: binder failure → retry → insufficient ─────────────────────────


async def test_dangling_citation_retries_once_then_succeeds(
    intent_json: str, retrieval: FakeRetrieval
) -> None:
    gateway = FakeGateway(
        generate_by_role={"generator.fast": intent_json},
        stream_script=[DANGLING_SYNTHESIS, GOOD_SYNTHESIS],
    )

    result, recorder = await run(gateway=gateway, retrieval=retrieval)

    assert result.envelope.status is AnswerStatus.ok
    assert gateway.stream_calls == 2
    synth_trace = next(t for t in result.traces if t.stage == "synthesize")
    assert synth_trace.meta["retry_reason"] == "dangling_citation"
    assert synth_trace.outcome is StageOutcome.retried
    # The failed attempt emitted nothing: no half-answer reached the client.
    assert recorder.kinds.count("summary_segment") == 2


async def test_dangling_citation_twice_falls_back_to_insufficient_basis(
    intent_json: str, retrieval: FakeRetrieval
) -> None:
    gateway = FakeGateway(
        generate_by_role={"generator.fast": intent_json},
        stream_script=[DANGLING_SYNTHESIS, DANGLING_SYNTHESIS],
    )

    result, recorder = await run(gateway=gateway, retrieval=retrieval)

    assert result.envelope.status is AnswerStatus.insufficient_basis
    assert [s.kind for s in result.envelope.summary_segments] == [
        SegmentKind.missing_info,
        SegmentKind.next_step,
    ]
    assert result.envelope.flags[0].code == "insufficient_basis"
    assert recorder.events[-1]["done"]["status"] == "insufficient_basis"
    # No `evidence` segment ever reached the client.
    assert "summary_segment" in recorder.kinds
    assert all(
        e["segment"]["kind"] != "evidence"
        for e in recorder.events
        if e["event"] in ("summary_segment", "detail_segment")
    )


async def test_uncited_evidence_line_is_a_structural_failure(
    intent_json: str, retrieval: FakeRetrieval
) -> None:
    gateway = FakeGateway(
        generate_by_role={"generator.fast": intent_json},
        stream_script=[UNCITED_SYNTHESIS, GOOD_SYNTHESIS],
    )

    result, _ = await run(gateway=gateway, retrieval=retrieval)

    synth_trace = next(t for t in result.traces if t.stage == "synthesize")
    assert synth_trace.meta["retry_reason"] == "uncited_evidence_segment"
    assert result.envelope.status is AnswerStatus.ok


async def test_retry_prompt_restates_the_violated_rule(
    intent_json: str, retrieval: FakeRetrieval
) -> None:
    gateway = FakeGateway(
        generate_by_role={"generator.fast": intent_json},
        stream_script=[DANGLING_SYNTHESIS, GOOD_SYNTHESIS],
    )

    await run(gateway=gateway, retrieval=retrieval)

    synthesis_prompts = [p for role, p in gateway.prompts_seen if role == "generator.heavy"]
    assert len(synthesis_prompts) == 2
    assert "cited an evidence block that does not exist" in synthesis_prompts[1]
    # Constrained retry: the task itself is unchanged.
    assert synthesis_prompts[1].startswith(synthesis_prompts[0])


# ── TC-7: degradation ───────────────────────────────────────────────────


async def test_web_unavailable_degrades_to_corpus_only_and_still_answers(
    intent_json: str,
) -> None:
    retrieval = FakeRetrieval(
        corpus_passages=[
            corpus_passage(1, "Amoxicillin is first line."),
            corpus_passage(2, "Five days of therapy."),
        ],
        web_error=RetrievalUnavailableError("searxng down"),
    )
    gateway = FakeGateway(
        generate_by_role={"generator.fast": intent_json}, stream_script=[GOOD_SYNTHESIS]
    )

    result, recorder = await run(gateway=gateway, retrieval=retrieval)

    assert result.envelope.status is AnswerStatus.ok
    assert [f.code for f in result.envelope.flags] == ["web_unavailable"]
    assert recorder.events[-1]["done"]["degraded"] is True
    assert "late_source" not in recorder.kinds
    web_trace = next(t for t in result.traces if t.stage == "retrieve_web")
    assert web_trace.outcome is StageOutcome.failed
    assert web_trace.meta["reason"].startswith("web_unavailable")


async def test_corpus_retrieval_down_is_insufficient_not_a_crash(intent_json: str) -> None:
    retrieval = FakeRetrieval(corpus_error=RetrievalUnavailableError("opensearch down"))
    gateway = FakeGateway(
        generate_by_role={"generator.fast": intent_json}, stream_script=[GOOD_SYNTHESIS]
    )

    result, recorder = await run(gateway=gateway, retrieval=retrieval)

    assert result.envelope.status is AnswerStatus.insufficient_basis
    assert recorder.events[0]["header"]["degraded"] is True
    assert recorder.kinds[-1] == "done"


async def test_no_passages_anywhere_is_insufficient_basis(intent_json: str) -> None:
    retrieval = FakeRetrieval(corpus_passages=[])
    gateway = FakeGateway(
        generate_by_role={"generator.fast": intent_json}, stream_script=[GOOD_SYNTHESIS]
    )
    config = settings(EVA_ANSWER_WEB_ENABLED="false")

    result, _ = await run(gateway=gateway, retrieval=retrieval, config=config)

    assert result.envelope.status is AnswerStatus.insufficient_basis
    assert result.envelope.flags[0].message == "no_passages"


# ── QS1 at the pipeline level ───────────────────────────────────────────


async def test_web_call_carries_the_intent_and_never_the_question(
    gateway: FakeGateway, retrieval: FakeRetrieval
) -> None:
    question = "What is the empiric therapy for CAP in Mr Kovalenko, born 13.06.1985?"

    await run(gateway=gateway, retrieval=retrieval, question=question)

    web_call = next(c for c in retrieval.calls if "web" in c["sources"])
    assert web_call["intent"] is not None
    concepts = [c.text for c in web_call["intent"].concepts]
    assert concepts == ["community acquired pneumonia", "amoxicillin"]
    assert "Kovalenko" not in " ".join(concepts)
    assert "13.06.1985" not in " ".join(concepts)


async def test_unparseable_intent_skips_the_web_entirely(retrieval: FakeRetrieval) -> None:
    """A keyword fallback is question tokens, not de-identified concepts —
    QS1 keeps it inside the cluster."""
    gateway = FakeGateway(
        generate_by_role={"generator.fast": "not json at all"},
        stream_script=[GOOD_SYNTHESIS],
    )

    result, recorder = await run(gateway=gateway, retrieval=retrieval)

    assert all("web" not in call["sources"] for call in retrieval.calls)
    assert recorder.events[0]["header"]["late_sources_expected"] is False
    plan_trace = next(t for t in result.traces if t.stage == "plan")
    assert plan_trace.meta["notes"] == "web_skipped_fallback_intent"
    intent_trace = next(t for t in result.traces if t.stage == "intent")
    assert intent_trace.meta["note"].startswith("intent_unparsed")
    assert intent_trace.outcome is StageOutcome.retried
