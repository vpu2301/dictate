"""Telemetry intake — fire-and-forget."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    # Sprint 15: 'layer_c' = generative ghost-text events (request_id echoes
    # the inline-completion response's request_id). Default keeps every
    # pre-sprint-15 client byte-compatible.
    source: Literal["autocomplete", "layer_c"] = "autocomplete"

    @model_validator(mode="after")
    def _accept_needs_exactly_one_id(self) -> TelemetryRequest:
        # accepted: phrase_id XOR snippet_id. Other events may reference at
        # most one id (shown_only carries the top suggestion's id for the
        # roll-up's impression counting) — never both.
        if self.phrase_id is not None and self.snippet_id is not None:
            msg = "phrase_id and snippet_id are mutually exclusive"
            raise ValueError(msg)
        if self.source == "layer_c":
            # Completions are not corpus rows: an id here would corrupt the
            # phrase counters the roll-up maintains.
            if self.phrase_id is not None or self.snippet_id is not None:
                msg = "layer_c events must not carry phrase_id/snippet_id"
                raise ValueError(msg)
            return self
        if self.event == "accepted" and self.phrase_id is None and self.snippet_id is None:
            msg = "accepted requires phrase_id or snippet_id"
            raise ValueError(msg)
        return self


@router.post("/telemetry", status_code=status.HTTP_204_NO_CONTENT)
async def receive_telemetry(
    body: TelemetryRequest,
    claims: Annotated[Claims, Depends(requires("autocomplete.read", "phrase"))],
) -> Response:
    state = get_state()
    # Fire-and-forget doctrine: NOTHING past validation may surface to the
    # client — losing telemetry is acceptable, slowing a keystroke is not.
    try:
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
            body.source,
        )
        state.telemetry_buffer.append(row)
        state.telemetry_event_metric.add(1, {"event": body.event, "source": body.source})
    except Exception:  # noqa: BLE001
        logger.warning("telemetry.intake_dropped", exc_info=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
