"""Telemetry intake — fire-and-forget."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from auth import Claims

from ..deps import get_state, requires
from ..scrubber import scrub_context, scrub_prefix

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/autocomplete", tags=["autocomplete"])


class TelemetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    event: Literal["shown_only", "accepted", "rejected", "timeout"]
    prefix: str = Field(max_length=200)
    phrase_id: UUID | None = None
    snippet_id: UUID | None = None
    context: dict = Field(default_factory=dict)


@router.post("/telemetry", status_code=status.HTTP_204_NO_CONTENT)
async def receive_telemetry(
    body: TelemetryRequest,
    claims: Annotated[Claims, Depends(requires("report.read", "report"))],
) -> Response:
    state = get_state()
    scrubbed = scrub_prefix(body.prefix)
    state.telemetry_redaction_metric.add(
        sum(scrubbed.redactions.values()),
        {"patterns": ",".join(scrubbed.redactions.keys()) or "none"},
    )
    row = (
        claims.tid,
        claims.sub,
        body.request_id,
        body.event,
        body.phrase_id,
        body.snippet_id,
        scrubbed.text,
        json.dumps(scrub_context(body.context)),
    )
    state.telemetry_buffer.append(row)
    state.telemetry_event_metric.add(1, {"event": body.event})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
