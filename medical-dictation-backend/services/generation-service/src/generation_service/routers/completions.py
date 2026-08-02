"""POST /v1/completions/inline — Layer C ghost text (sprint 15, ADR-0036).

Flow: authz → feature gate → rate limit → (slot → inference) under the
hard end-to-end budget → safety filter → 200 or 204. Silence (204) is a
valid answer at every gate: a typing clinician must never see an error
because ghost text failed to materialise.

Scope: the sprint spec's ``autocomplete.suggest`` maps to the live
``("autocomplete.read", "phrase")`` permission (the same one the
suggest endpoint checks — see ADR-0036).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from audit import Severity
from auth import Claims

from .. import audit_kinds
from ..config import settings
from ..deps import get_state, requires
from ..domain.prompt import Language, build_prompt
from ..domain.safety_filter import check_completion

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/completions", tags=["completions"])


class InlineCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Context pointers only — carried into telemetry correlation, never
    # dereferenced here: the completion depends solely on the typed text,
    # and a foreign report_id resolves to nothing a caller couldn't already
    # see (RLS), so a DB round-trip would buy latency, not security.
    report_id: UUID
    section_key: str = Field(min_length=1, max_length=64)
    text_before_cursor: str = Field(min_length=1, max_length=1000)
    language: Language


class InlineCompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Echoed by SPA telemetry (shown/accepted/rejected) for correlation.
    request_id: UUID
    completion: str
    model: str
    latency_ms: int


def _no_completion(outcome: str) -> Response:
    get_state().completions_metric.add(1, {"outcome": outcome})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/inline",
    response_model=InlineCompletionResponse,
    responses={204: {"description": "No confident completion — silence is a valid answer"}},
)
async def inline_completion(
    body: InlineCompletionRequest,
    claims: Annotated[Claims, Depends(requires("autocomplete.read", "phrase"))],
) -> Response | InlineCompletionResponse:
    state = get_state()

    if not settings.layer_c_enabled or state.inference is None:
        return _no_completion("disabled")
    allowlist = settings.tenant_allowlist_set
    if allowlist and claims.tid not in allowlist:
        return _no_completion("tenant_disabled")

    allowed, retry_after = await state.rate_limiter.check(user_id=claims.sub)
    if not allowed:
        state.completions_metric.add(1, {"outcome": "rate_limited"})
        return Response(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(max(retry_after, 1))},
        )

    prompt = build_prompt(
        section_key=body.section_key,
        text_before_cursor=body.text_before_cursor,
        language=body.language,
    )

    started = time.perf_counter()
    try:
        # Slot wait is INSIDE the budget: if both slots are busy past the
        # deadline the request 204s instead of queueing a stale ghost.
        async with asyncio.timeout(settings.gen_timeout_ms / 1000.0):
            async with state.slot_pool:
                result = await state.inference.complete(
                    prompt=prompt, max_tokens=settings.gen_max_tokens
                )
    except TimeoutError:
        return _no_completion("timeout")
    except httpx.HTTPError as exc:
        logger.warning("generation.inline_backend_error: %s", exc)
        return _no_completion("backend_error")
    latency_ms = int((time.perf_counter() - started) * 1000)

    # Ghost text continues the sentence in place — leading ellipses/dashes
    # the model likes to emit («...плечі») would render mid-word garbage.
    completion = result.text.strip().lstrip(".…-–— ").rstrip()
    if not completion:
        return _no_completion("empty")

    verdict = check_completion(completion, text_before_cursor=body.text_before_cursor)
    if not verdict.allowed:
        state.completions_metric.add(1, {"outcome": "filtered", "reason": verdict.reason})
        await state.audit_writer.write_event(
            tenant_id=claims.tid,
            kind=audit_kinds.LAYER_C_COMPLETION_FILTERED,
            actor_sub=claims.sub,
            target_kind="report",
            target_id=body.report_id,
            payload={
                "section_key": body.section_key,
                "reason": verdict.reason,
                "matched": verdict.matched,
                "language": body.language,
            },
            severity=Severity.WARN,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    state.completions_metric.add(1, {"outcome": "served"})
    state.inline_latency_metric.record(latency_ms)
    await state.shown_audit.record(tenant_id=claims.tid)
    return InlineCompletionResponse(
        request_id=uuid4(),
        completion=completion,
        model=result.model,
        latency_ms=latency_ms,
    )
