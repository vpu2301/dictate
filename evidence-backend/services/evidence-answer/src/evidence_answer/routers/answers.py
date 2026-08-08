"""POST /answers (SSE) and GET /answers/{id}.

The stream runs the pipeline in a background task feeding a queue, and the
response generator drains that queue while emitting heartbeats. Doing it the
obvious way — `async for` straight off the pipeline — would stall the
connection during a slow web fetch and let an intermediary time it out.

Persistence happens *after* `done` is queued and before the generator closes:
the clinician has already read the answer, so a slow write must not delay
them, but the answer must still exist by the time they could click reopen.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated
from uuid import UUID, uuid4

from auth import Claims
from auth.perms import Action, TargetKind
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from evidence_answer.adapters import pg
from evidence_answer.config import settings
from evidence_answer.deps import requires, user_connection
from evidence_answer.domain import sse
from evidence_answer.domain.pipeline import PipelineDeps, QuickSearchPipeline
from evidence_answer.domain.stages.intake import InFlightSlot, PipelineOverloadedError
from evidence_answer.main_deps import get_state
from evidence_models import AnswerEnvelope, AnswerMode

logger = logging.getLogger(__name__)
router = APIRouter(tags=["answers"])

_ASK: Action = "evidence.ask"
_TARGET: TargetKind = "evidence"

# Sentinel pushed onto the queue when the pipeline is finished.
_END = b""


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=2000)
    mode: AnswerMode = AnswerMode.quick_search
    locale: str = Field(default="uk", pattern=r"^(uk|en)(-[A-Za-z]{2})?$")


@router.post("/answers")
async def ask(
    body: AskRequest,
    claims: Annotated[Claims, Depends(requires(_ASK, _TARGET))],
) -> StreamingResponse:
    state = get_state()
    if body.mode is not AnswerMode.quick_search:
        # The other modes arrive in S05/S09/S10. Saying so is better than
        # quietly answering a contextual question without context.
        raise HTTPException(status_code=422, detail=f"mode_unsupported: {body.mode.value}")

    slot = InFlightSlot(
        state.redis,
        tenant_id=claims.tid,
        user_sub=claims.sub,
        limit=settings.max_inflight_per_user,
        ttl_seconds=settings.inflight_ttl_seconds,
    )
    try:
        await slot.__aenter__()
    except PipelineOverloadedError as exc:
        raise HTTPException(status_code=429, detail=f"pipeline_overloaded: {exc}") from exc

    answer_id = uuid4()
    try:
        async with user_connection(
            state.app_pool, tenant_id=claims.tid, user_sub=claims.sub
        ) as conn:
            question_id = await pg.insert_question(
                conn,
                tenant_id=claims.tid,
                user_sub=claims.sub,
                text=body.question,
                mode=body.mode.value,
                locale=body.locale,
            )
    except Exception:
        await slot.__aexit__(None, None, None)
        raise

    queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def emit(frame: bytes) -> None:
        await queue.put(frame)

    async def run() -> None:
        pipeline = QuickSearchPipeline(
            PipelineDeps(settings=settings, gateway=state.gateway, retrieval=state.retrieval)
        )
        try:
            result = await pipeline.run(
                question=body.question,
                locale=body.locale,
                tenant_id=claims.tid,
                question_id=question_id,
                answer_id=answer_id,
                emit=emit,
            )
        except Exception as exc:  # noqa: BLE001 — the stream must always close
            logger.exception("answers.pipeline_failed")
            await queue.put(sse.error_event("pipeline_failed", type(exc).__name__, retryable=True))
            await queue.put(_END)
            return

        try:
            if result.deflected:
                await state.persister.audit_deflection(
                    tenant_id=claims.tid,
                    user_sub=claims.sub,
                    actor_role=(claims.roles[0] if claims.roles else None),
                    question_id=question_id,
                    reason_code=result.deflection_reason,
                    matched_rule=result.deflection_rule,
                    classifier_used=result.deflection_classifier,
                )
                state.metrics.deflections[result.deflection_reason] = (
                    state.metrics.deflections.get(result.deflection_reason, 0) + 1
                )

            def factory() -> object:
                return user_connection(state.app_pool, tenant_id=claims.tid, user_sub=claims.sub)

            await state.persister.persist(
                conn_factory=factory,
                tenant_id=claims.tid,
                user_sub=claims.sub,
                actor_role=(claims.roles[0] if claims.roles else None),
                question_id=question_id,
                envelope=result.envelope,
                provenance=result.provenance,
                traces=result.traces,
                connectors=result.connectors,
            )
            state.metrics.answers[result.envelope.status.value] = (
                state.metrics.answers.get(result.envelope.status.value, 0) + 1
            )
        except Exception:
            # The content already streamed; the client has it. What failed is
            # durability, and the honest signal is a trailing error event so
            # the SPA disables "reopen" rather than 404ing on it later.
            logger.exception("answers.persist_failed")
            await queue.put(
                sse.error_event(
                    "answer_not_persisted",
                    "the answer was generated but could not be stored",
                    retryable=True,
                )
            )
        await queue.put(_END)

    task = asyncio.create_task(run())

    async def stream() -> asyncio.AsyncIterator[bytes]:  # type: ignore[name-defined]
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=settings.heartbeat_seconds)
                except TimeoutError:
                    yield sse.HEARTBEAT_FRAME
                    continue
                if frame == _END:
                    return
                yield frame
        finally:
            if not task.done():
                # Client hung up: the pipeline is finishing an answer nobody
                # is reading. Let it finish — it still persists, so the user
                # can reopen what they disconnected from.
                logger.info("answers.client_disconnected", extra={"answer_id": str(answer_id)})
            await slot.__aexit__(None, None, None)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=sse.stream_headers(answer_id),
    )


@router.get("/answers/{answer_id}")
async def get_answer(
    answer_id: UUID,
    claims: Annotated[Claims, Depends(requires(_ASK, _TARGET))],
) -> AnswerEnvelope:
    """Reopen: served from the stored envelope object.

    Web passages come from the page snapshot captured at answer time — the
    fetcher is not called, so reopening an answer neither re-fetches nor
    re-dates a citation (AC-S04-B-6).
    """
    state = get_state()
    async with user_connection(state.app_pool, tenant_id=claims.tid, user_sub=claims.sub) as conn:
        key = await pg.get_answer_envelope_ref(conn, tenant_id=claims.tid, answer_id=answer_id)
    # RLS makes another user's (or tenant's) answer invisible, which surfaces
    # here as not-found — never "forbidden" (rule PD3).
    if key is None:
        raise HTTPException(status_code=404, detail="not_found")
    return await state.persister.load_envelope(tenant_id=claims.tid, key=key)
