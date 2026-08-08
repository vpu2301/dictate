"""Answer stream contract v1 (S04, `POST /answers` SSE).

The stream is a public interface in exactly the way `AnswerEnvelope` is: the
SPA is written against THIS shape and the pipeline bends to it, never the
other way round (spec D1). Event order is fixed:

    header → summary_segment* → detail_segment* → source* → late_source* → done

`error` may replace the tail at any point. A deflected question emits
`header` then `done` with `status=deflected` and a populated `triage` payload —
deflection is an answer state, not a transport error (rule SC3/CS2).

Reopening a dropped stream is `GET /answers/{id}`, which returns the same
content as a complete `AnswerEnvelope`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import model_validator

from .common import ContractVersion, StrictModel
from .envelope import AnswerStatus, Flag, Segment, SourceRef
from .triage import TriageDecision


class StreamEventKind(StrEnum):
    header = "header"
    summary_segment = "summary_segment"
    detail_segment = "detail_segment"
    source = "source"
    late_source = "late_source"
    done = "done"
    error = "error"


class AnswerMode(StrEnum):
    """Product modes; the DB CHECK on questions.mode carries the same values."""

    quick_search = "quick_search"
    contextual = "contextual"
    deeptrace = "deeptrace"
    drug = "drug"


class AnswerStreamHeader(StrictModel):
    """First event. Everything the client needs to render a shell before a
    single token of content exists."""

    answer_id: UUID
    question_id: UUID
    mode: AnswerMode
    locale: str
    # False until S06 verification runs; the SPA renders the unverified banner.
    verified: bool = False
    # Web connectors are still fetching: more `late_source` events may follow
    # the first `done`-bound sources (spec D7).
    late_sources_expected: bool = False
    # A connector the plan asked for could not serve (e.g. web_unavailable).
    degraded: bool = False
    flags: list[Flag] = []
    pipeline_version: str
    contract_version: ContractVersion = "1.0"


class AnswerStreamDone(StrictModel):
    """Terminal event of a successful (or deflected/insufficient) stream."""

    answer_id: UUID
    status: AnswerStatus
    provenance_ref: str
    summary_count: int = 0
    detail_count: int = 0
    source_count: int = 0
    flags: list[Flag] = []
    degraded: bool = False
    # Populated iff status is `deflected`.
    triage: TriageDecision | None = None


class AnswerStreamError(StrictModel):
    """Terminal event of a failed stream. `code` matches the RFC 7807 code the
    equivalent non-streaming problem response would carry (rule BE2)."""

    code: str
    detail: str
    # True when re-asking the same question may succeed (transient upstream).
    retryable: bool = False


_PAYLOAD_FIELDS: tuple[str, ...] = ("header", "segment", "source", "done", "error")

_PAYLOAD_FOR: dict[StreamEventKind, str] = {
    StreamEventKind.header: "header",
    StreamEventKind.summary_segment: "segment",
    StreamEventKind.detail_segment: "segment",
    StreamEventKind.source: "source",
    StreamEventKind.late_source: "source",
    StreamEventKind.done: "done",
    StreamEventKind.error: "error",
}


class AnswerStreamEvent(StrictModel):
    """Wire union. Exactly one payload field is set, selected by `event`.

    SSE framing: the `event:` line carries `event`, the `data:` line carries
    this model serialized as JSON.
    """

    event: StreamEventKind
    header: AnswerStreamHeader | None = None
    segment: Segment | None = None
    source: SourceRef | None = None
    done: AnswerStreamDone | None = None
    error: AnswerStreamError | None = None

    @model_validator(mode="after")
    def _exactly_the_matching_payload(self) -> Self:
        expected = _PAYLOAD_FOR[self.event]
        for field in _PAYLOAD_FIELDS:
            value = getattr(self, field)
            if field == expected and value is None:
                raise ValueError(f"event {self.event.value!r} requires the {field!r} payload")
            if field != expected and value is not None:
                raise ValueError(f"event {self.event.value!r} must not carry the {field!r} payload")
        return self
