"""The Quick Search pipeline: the stage graph and its trace.

    intake → triage → intent → plan → retrieve → synthesize → persist

Every stage records a `StageTrace` (rule BE5): start, end, outcome, and a meta
dict small enough to be safe to store. The traces are what make a bad answer
diagnosable six weeks later, so they are written even when the pipeline ends
in `insufficient_basis` or a deflection.

Concurrency shape (spec D7): the corpus retrieval is awaited, then synthesis
starts streaming; the web retrieval runs as a background task the whole time
and is joined *after* synthesis, contributing `late_source` events and
provenance entries. The clinician reads a cited corpus answer while a third
party's server is still deciding whether to answer us.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from evidence_answer.adapters.retrieval import (
    RetrievalClient,
    RetrievalUnavailableError,
    connector_ids,
    dedupe_passages,
)
from evidence_answer.config import Settings
from evidence_answer.domain import insufficient, prompts, sse
from evidence_answer.domain.binder import BinderError, EvidenceBlock, cited_block_ids
from evidence_answer.domain.stages import intent as intent_stage
from evidence_answer.domain.stages import plan as plan_stage
from evidence_answer.domain.stages import synthesize as synth_stage
from evidence_answer.domain.stages import triage as triage_stage
from evidence_models import (
    AnswerEnvelope,
    AnswerMode,
    AnswerProvenance,
    AnswerStatus,
    AnswerStreamDone,
    AnswerStreamHeader,
    ClinicalIntent,
    EvidencePassage,
    Flag,
    FlagSeverity,
    RetrieveResponse,
    Segment,
    SegmentKind,
    SourceKind,
    SourceRef,
    StageOutcome,
    StageTrace,
    TriageDecision,
    TriageOutcome,
)
from models import ModelGatewayClient

logger = logging.getLogger(__name__)

# Emitted frames go through this queue so the route can interleave heartbeats.
Emit = Callable[[bytes], Awaitable[None]]


@dataclass(slots=True)
class TraceRecorder:
    traces: list[StageTrace] = field(default_factory=list)

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[dict[str, str]]:
        """Record one stage. The yielded dict is the stage's `meta`.

        A stage may downgrade its own outcome by setting `meta["outcome"]`
        (e.g. `retried`). An exception always wins: a stage that blew up must
        not be able to report itself as fine.
        """
        started = datetime.now(tz=UTC)
        meta: dict[str, str] = {}
        failed = False
        try:
            yield meta
        except Exception:
            failed = True
            raise
        finally:
            declared = meta.pop("outcome", StageOutcome.ok.value)
            self.traces.append(
                StageTrace(
                    stage=name,
                    started_at=started,
                    ended_at=datetime.now(tz=UTC),
                    outcome=StageOutcome.failed if failed else StageOutcome(declared),
                    meta=meta,
                )
            )


@dataclass(slots=True)
class PipelineDeps:
    settings: Settings
    gateway: ModelGatewayClient
    retrieval: RetrievalClient


@dataclass(slots=True)
class PipelineResult:
    envelope: AnswerEnvelope
    provenance: AnswerProvenance
    traces: list[StageTrace]
    connectors: list[str]
    deflected: bool = False
    deflection_reason: str = ""
    deflection_rule: str | None = None
    deflection_classifier: bool = False


def _source_ref(block: EvidenceBlock) -> SourceRef:
    passage = block.passage
    if passage.web_ref is not None:
        return SourceRef(
            id=block.source_id,
            kind=SourceKind.web,
            title=passage.section_path or passage.web_ref.domain,
            evidence_tier=passage.evidence_tier,
            source_authority=passage.source_authority,
            web=passage.web_ref,
        )
    return SourceRef(
        id=block.source_id,
        kind=SourceKind.corpus,
        title=passage.section_path or f"passage {passage.id}",
        evidence_tier=passage.evidence_tier,
        source_authority=passage.source_authority,
        document_version_id=passage.document_version_id,
    )


class QuickSearchPipeline:
    def __init__(self, deps: PipelineDeps) -> None:
        self._deps = deps

    async def run(
        self,
        *,
        question: str,
        locale: str,
        tenant_id: UUID,
        question_id: UUID,
        answer_id: UUID,
        emit: Emit,
    ) -> PipelineResult:
        settings = self._deps.settings
        recorder = TraceRecorder()
        flags: list[Flag] = []
        started_at = datetime.now(tz=UTC)

        # ── triage ──────────────────────────────────────────────────────
        with recorder.stage("triage") as meta:
            decision, note = await triage_stage.triage(
                question,
                locale=locale,
                gateway=self._deps.gateway,
                prompt=prompts.triage_prompt().text,
                classifier_enabled=settings.triage_classifier_enabled,
            )
            meta["note"] = note
            meta["outcome_kind"] = decision.outcome.value

        if decision.outcome is TriageOutcome.deflect:
            return await self._deflect(
                decision=decision,
                answer_id=answer_id,
                question_id=question_id,
                locale=locale,
                recorder=recorder,
                started_at=started_at,
                emit=emit,
            )

        # ── intent ──────────────────────────────────────────────────────
        with recorder.stage("intent") as meta:
            intent, note = await intent_stage.extract_intent(
                question, gateway=self._deps.gateway, prompt=prompts.intent_prompt().text
            )
            fallback = note.startswith("intent_unparsed")
            meta["note"] = note
            meta["concepts"] = str(len(intent.concepts))
            if fallback:
                meta["outcome"] = StageOutcome.retried.value

        # ── plan ────────────────────────────────────────────────────────
        with recorder.stage("plan") as meta:
            search_plan = plan_stage.plan_quick_search(
                intent=intent,
                question=question,
                tenant_id=tenant_id,
                locale=locale,
                k=settings.corpus_k,
                web_k=settings.web_k,
                web_enabled=settings.web_enabled,
                intent_is_fallback=fallback,
            )
            meta["sources"] = ",".join(search_plan.corpus.sources)
            meta["web"] = str(search_plan.expects_web).lower()
            if search_plan.notes:
                meta["notes"] = ",".join(search_plan.notes)

        # Web retrieval starts NOW and runs while corpus retrieval and
        # synthesis proceed — the whole point of the late-source design.
        web_task: asyncio.Task[list[EvidencePassage]] | None = None
        if search_plan.web is not None:
            web_task = asyncio.create_task(self._retrieve_web(search_plan.web))

        header = AnswerStreamHeader(
            answer_id=answer_id,
            question_id=question_id,
            mode=AnswerMode.quick_search,
            locale=locale,
            verified=False,  # until S06
            late_sources_expected=web_task is not None,
            degraded=False,
            pipeline_version=settings.pipeline_version,
        )

        # ── retrieve (corpus) ───────────────────────────────────────────
        degraded = False
        connectors: list[str] = []
        corpus_passages: list[EvidencePassage] = []
        try:
            with recorder.stage("retrieve_corpus") as meta:
                response = await self._retrieve(
                    search_plan.corpus, timeout_s=settings.corpus_timeout_s
                )
                corpus_passages = response.passages
                connectors.extend(connector_ids(response.connector_meta))
                degraded = degraded or response.degraded
                meta["passages"] = str(len(corpus_passages))
                meta["degraded"] = str(response.degraded).lower()
        except RetrievalUnavailableError as exc:
            logger.warning("pipeline.retrieval_unavailable", extra={"error": str(exc)})
            header.degraded = True
            await emit(sse.header_event(header))
            return await self._insufficient(
                reason="retrieval_unavailable",
                answer_id=answer_id,
                question_id=question_id,
                locale=locale,
                recorder=recorder,
                started_at=started_at,
                emit=emit,
                intent=intent,
                connectors=connectors,
                degraded=True,
                emit_header=False,
            )

        header.degraded = degraded
        await emit(sse.header_event(header))

        if not corpus_passages and web_task is None:
            return await self._insufficient(
                reason="no_passages",
                answer_id=answer_id,
                question_id=question_id,
                locale=locale,
                recorder=recorder,
                started_at=started_at,
                emit=emit,
                intent=intent,
                connectors=connectors,
                degraded=degraded,
                emit_header=False,
            )

        # ── synthesize ──────────────────────────────────────────────────
        passages = corpus_passages[: settings.max_evidence_blocks]

        async def on_segment(placement: str, segment: Segment) -> None:
            await emit(sse.segment_event(placement, segment))

        try:
            with recorder.stage("synthesize") as meta:
                synthesis = await synth_stage.synthesize(
                    gateway=self._deps.gateway,
                    template=prompts.synthesis_prompt().text,
                    question=question,
                    passages=passages,
                    max_tokens=settings.synthesis_max_tokens,
                    temperature=settings.synthesis_temperature,
                    on_segment=on_segment,
                )
                meta["attempts"] = str(synthesis.attempts)
                meta["summary"] = str(len(synthesis.summary))
                meta["detail"] = str(len(synthesis.detail))
                if synthesis.retry_reason:
                    meta["retry_reason"] = synthesis.retry_reason
                    meta["outcome"] = StageOutcome.retried.value
                if synthesis.dropped_lines:
                    meta["dropped_lines"] = str(len(synthesis.dropped_lines))
                if synthesis.partial_reason:
                    meta["partial_reason"] = synthesis.partial_reason
        except (BinderError, synth_stage.SynthesisUnavailableError) as exc:
            reason = "binder_failed" if isinstance(exc, BinderError) else "synthesis_unavailable"
            if web_task is not None:
                web_task.cancel()
            return await self._insufficient(
                reason=reason,
                answer_id=answer_id,
                question_id=question_id,
                locale=locale,
                recorder=recorder,
                started_at=started_at,
                emit=emit,
                intent=intent,
                connectors=connectors,
                degraded=degraded,
                emit_header=False,
            )

        if synthesis.partial_reason:
            flags.append(
                Flag(
                    code="partial_synthesis",
                    severity=FlagSeverity.warning,
                    message=synthesis.partial_reason,
                )
            )

        # ── sources (corpus) ────────────────────────────────────────────
        cited = cited_block_ids(synthesis.summary, synthesis.detail)
        sources = [_source_ref(b) for b in synthesis.blocks if b.source_id in cited]
        for source in sources:
            await emit(sse.source_event(source))

        # ── late web sources ────────────────────────────────────────────
        web_passages: list[EvidencePassage] = []
        if web_task is not None:
            with recorder.stage("retrieve_web") as meta:
                try:
                    web_passages = await asyncio.wait_for(web_task, timeout=settings.web_timeout_s)
                    meta["passages"] = str(len(web_passages))
                except (TimeoutError, asyncio.CancelledError):
                    web_task.cancel()
                    meta["outcome"] = StageOutcome.failed.value
                    meta["reason"] = "web_timeout"
                    degraded = True
                except RetrievalUnavailableError as exc:
                    # `web_unavailable` never errors the answer (spec §3): the
                    # corpus answer already streamed, the header said web was
                    # coming, and now it honestly is not.
                    meta["outcome"] = StageOutcome.failed.value
                    meta["reason"] = f"web_unavailable:{type(exc).__name__}"
                    degraded = True

            if web_passages:
                connectors.append("web@evidence-websearch")
                late = self._late_sources(web_passages, offset=len(synthesis.blocks))
                for source in late:
                    await emit(sse.source_event(source, late=True))
                sources.extend(late)
            elif search_plan.expects_web:
                flags.append(
                    Flag(
                        code="web_unavailable",
                        severity=FlagSeverity.info,
                        message="live web sources were requested but returned nothing",
                    )
                )

        # ── envelope ────────────────────────────────────────────────────
        envelope = AnswerEnvelope(
            answer_id=answer_id,
            status=AnswerStatus.ok,
            summary_segments=synthesis.summary,
            detail_segments=synthesis.detail,
            sources=sources,
            flags=flags,
            provenance_ref=str(answer_id),
        )
        provenance = self._provenance(
            answer_id=answer_id,
            question_id=question_id,
            passages=dedupe_passages(passages, web_passages),
            connectors=sorted(set(connectors)),
            started_at=started_at,
        )
        await emit(
            sse.done_event(
                AnswerStreamDone(
                    answer_id=answer_id,
                    status=AnswerStatus.ok,
                    provenance_ref=envelope.provenance_ref,
                    summary_count=len(envelope.summary_segments),
                    detail_count=len(envelope.detail_segments),
                    source_count=len(envelope.sources),
                    flags=flags,
                    degraded=degraded,
                )
            )
        )
        return PipelineResult(
            envelope=envelope,
            provenance=provenance,
            traces=recorder.traces,
            connectors=sorted(set(connectors)),
        )

    # ── helpers ─────────────────────────────────────────────────────────

    async def _retrieve(
        self, retrieval_plan: plan_stage.RetrievalPlan, *, timeout_s: float
    ) -> RetrieveResponse:
        """The single place a `RetrievalPlan` becomes a retrieval call.

        `intent` travels only because `plan.py` put it on the plan — this
        method never invents one, which is what keeps QS1 a property of the
        planner rather than of every call site.
        """
        return await self._deps.retrieval.retrieve(
            query=retrieval_plan.query,
            sources=retrieval_plan.sources,
            k=retrieval_plan.k,
            tenant_id=retrieval_plan.tenant_id,
            timeout_s=timeout_s,
            locale=retrieval_plan.locale,
            snapshot_id=retrieval_plan.snapshot_id,
            intent=retrieval_plan.intent,
        )

    async def _retrieve_web(self, web_plan: plan_stage.RetrievalPlan) -> list[EvidencePassage]:
        response = await self._retrieve(web_plan, timeout_s=self._deps.settings.web_timeout_s)
        return response.passages

    def _late_sources(self, passages: list[EvidencePassage], *, offset: int) -> list[SourceRef]:
        """Web sources arrive after synthesis, so no segment cites them.

        They are listed as contributing context with distinct ids (`W1`, `W2`)
        rather than being folded into the `S`-space, which belongs to blocks
        the model actually saw.
        """
        seen: set[str] = set()
        out: list[SourceRef] = []
        for passage in passages:
            if passage.web_ref is None or passage.web_ref.url in seen:
                continue
            seen.add(passage.web_ref.url)
            out.append(
                SourceRef(
                    id=f"W{len(out) + 1}",
                    kind=SourceKind.web,
                    title=passage.section_path or passage.web_ref.domain,
                    web=passage.web_ref,
                )
            )
        return out

    def _provenance(
        self,
        *,
        answer_id: UUID,
        question_id: UUID,
        passages: list[EvidencePassage],
        connectors: list[str],
        started_at: datetime,
    ) -> AnswerProvenance:
        settings = self._deps.settings
        return AnswerProvenance(
            answer_id=answer_id,
            question_ref=str(question_id),
            snapshot_hash=None,  # no patient context in quick mode (S05)
            consumed_fields=[],
            passage_ids=[p.id for p in passages],
            web_refs=[p.web_ref for p in passages if p.web_ref is not None],
            connectors=connectors,
            model_pins={
                "generator.fast": intent_stage.EXTRACTOR_ROLE,
                "generator.heavy": synth_stage.SYNTHESIS_ROLE,
                "temperature": str(settings.synthesis_temperature),
            },
            prompt_versions=prompts.all_versions(),
            pipeline_version=settings.pipeline_version,
            build_version=settings.build_version,
            created_at=started_at,
        )

    async def _deflect(
        self,
        *,
        decision: TriageDecision,
        answer_id: UUID,
        question_id: UUID,
        locale: str,
        recorder: TraceRecorder,
        started_at: datetime,
        emit: Emit,
    ) -> PipelineResult:
        reason = decision.reason_code.value if decision.reason_code else "out_of_scope"
        header = AnswerStreamHeader(
            answer_id=answer_id,
            question_id=question_id,
            mode=AnswerMode.quick_search,
            locale=locale,
            pipeline_version=self._deps.settings.pipeline_version,
        )
        await emit(sse.header_event(header))
        # The safe-messaging text is the answer: a `next_step` segment, so the
        # SPA renders it through the same segment component as everything else
        # (rule FE7) instead of a bespoke error card.
        segment = Segment(
            id="seg-1",
            kind=SegmentKind.next_step,
            text=decision.message or "",
        )
        await emit(sse.segment_event("summary", segment))
        envelope = AnswerEnvelope(
            answer_id=answer_id,
            status=AnswerStatus.deflected,
            summary_segments=[segment],
            flags=[
                Flag(
                    code=f"triage_{reason}",
                    severity=FlagSeverity.critical
                    if reason in ("emergency", "self_harm")
                    else FlagSeverity.info,
                    message=reason,
                    requires_ack=reason in ("emergency", "self_harm"),
                )
            ],
            provenance_ref=str(answer_id),
        )
        await emit(
            sse.done_event(
                AnswerStreamDone(
                    answer_id=answer_id,
                    status=AnswerStatus.deflected,
                    provenance_ref=envelope.provenance_ref,
                    summary_count=1,
                    flags=envelope.flags,
                    triage=decision,
                )
            )
        )
        return PipelineResult(
            envelope=envelope,
            provenance=self._provenance(
                answer_id=answer_id,
                question_id=question_id,
                passages=[],
                connectors=[],
                started_at=started_at,
            ),
            traces=recorder.traces,
            connectors=[],
            deflected=True,
            deflection_reason=reason,
            deflection_rule=decision.matched_rule,
            deflection_classifier=decision.classifier_used,
        )

    async def _insufficient(
        self,
        *,
        reason: str,
        answer_id: UUID,
        question_id: UUID,
        locale: str,
        recorder: TraceRecorder,
        started_at: datetime,
        emit: Emit,
        intent: ClinicalIntent | None,
        connectors: list[str],
        degraded: bool,
        emit_header: bool = True,
    ) -> PipelineResult:
        if emit_header:
            await emit(
                sse.header_event(
                    AnswerStreamHeader(
                        answer_id=answer_id,
                        question_id=question_id,
                        mode=AnswerMode.quick_search,
                        locale=locale,
                        degraded=degraded,
                        pipeline_version=self._deps.settings.pipeline_version,
                    )
                )
            )
        segments, flags = insufficient.compose(reason, locale=locale)
        for segment in segments:
            await emit(sse.segment_event("summary", segment))
        envelope = AnswerEnvelope(
            answer_id=answer_id,
            status=AnswerStatus.insufficient_basis,
            summary_segments=segments,
            flags=flags,
            provenance_ref=str(answer_id),
        )
        await emit(
            sse.done_event(
                AnswerStreamDone(
                    answer_id=answer_id,
                    status=AnswerStatus.insufficient_basis,
                    provenance_ref=envelope.provenance_ref,
                    summary_count=len(segments),
                    flags=flags,
                    degraded=degraded,
                )
            )
        )
        return PipelineResult(
            envelope=envelope,
            provenance=self._provenance(
                answer_id=answer_id,
                question_id=question_id,
                passages=[],
                connectors=connectors,
                started_at=started_at,
            ),
            traces=recorder.traces,
            connectors=connectors,
        )


def new_answer_id() -> UUID:
    return uuid4()
