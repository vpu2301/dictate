"""SSE framing for the answer stream.

The wire contract is `evidence_models.AnswerStreamEvent` — the model validates
that each event carries exactly its own payload, so a framing bug is a
`ValidationError` at the source rather than a confused client.

Framing notes that matter to the SPA:

* `event:` carries the event kind, `data:` carries the whole `AnswerStreamEvent`
  as JSON. Clients may switch on either; both always agree.
* A comment heartbeat (`: ping`) keeps intermediaries from closing an idle
  connection during a long web fetch. Comments are invisible to EventSource.
* `retry:` is deliberately NOT sent: on a drop the client resumes with
  `GET /answers/{id}` (which returns the finished envelope), rather than
  re-running a pipeline that has already cost real tokens.
"""

from __future__ import annotations

from uuid import UUID

from evidence_models import (
    AnswerStreamDone,
    AnswerStreamError,
    AnswerStreamEvent,
    AnswerStreamHeader,
    Segment,
    SourceRef,
    StreamEventKind,
)

HEARTBEAT_FRAME = b": ping\n\n"


def encode(event: AnswerStreamEvent) -> bytes:
    payload = event.model_dump_json(exclude_none=True)
    return f"event: {event.event.value}\ndata: {payload}\n\n".encode()


def header_event(header: AnswerStreamHeader) -> bytes:
    return encode(AnswerStreamEvent(event=StreamEventKind.header, header=header))


def segment_event(placement: str, segment: Segment) -> bytes:
    kind = (
        StreamEventKind.summary_segment
        if placement == "summary"
        else StreamEventKind.detail_segment
    )
    return encode(AnswerStreamEvent(event=kind, segment=segment))


def source_event(source: SourceRef, *, late: bool = False) -> bytes:
    kind = StreamEventKind.late_source if late else StreamEventKind.source
    return encode(AnswerStreamEvent(event=kind, source=source))


def done_event(done: AnswerStreamDone) -> bytes:
    return encode(AnswerStreamEvent(event=StreamEventKind.done, done=done))


def error_event(code: str, detail: str, *, retryable: bool = False) -> bytes:
    return encode(
        AnswerStreamEvent(
            event=StreamEventKind.error,
            error=AnswerStreamError(code=code, detail=detail, retryable=retryable),
        )
    )


def stream_headers(answer_id: UUID) -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Connection": "keep-alive",
        # Nginx/Envoy buffer SSE by default, which turns a streamed answer
        # into a single late blob.
        "X-Accel-Buffering": "no",
        # Lets a client that lost the stream resume without parsing the body.
        "X-Answer-Id": str(answer_id),
    }
