"""SSE framing, the insufficient-basis composer, prompt loading, and the
in-flight concurrency cap."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from evidence_answer.domain import insufficient, prompts, sse
from evidence_answer.domain.stages.intake import InFlightSlot, PipelineOverloadedError
from pydantic import ValidationError

from evidence_models import (
    AnswerMode,
    AnswerStatus,
    AnswerStreamDone,
    AnswerStreamEvent,
    AnswerStreamHeader,
    Segment,
    SegmentKind,
    SourceKind,
    SourceRef,
    StreamEventKind,
)

HEADER = AnswerStreamHeader(
    answer_id=uuid4(),
    question_id=uuid4(),
    mode=AnswerMode.quick_search,
    locale="uk",
    pipeline_version="quick-search-1.0",
)


def decode(frame: bytes) -> tuple[str, dict]:
    lines = frame.decode().splitlines()
    event = next(line[7:] for line in lines if line.startswith("event: "))
    data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
    return event, data


# ── SSE framing ─────────────────────────────────────────────────────────


def test_event_line_and_data_payload_always_agree() -> None:
    event, data = decode(sse.header_event(HEADER))
    assert event == "header" == data["event"]


def test_frames_are_terminated_with_a_blank_line() -> None:
    """Without the double newline the client buffers forever."""
    assert sse.header_event(HEADER).endswith(b"\n\n")


def test_segment_placement_selects_the_event_kind() -> None:
    segment = Segment(id="seg-1", kind=SegmentKind.uncertainty, text="Unclear.")

    assert decode(sse.segment_event("summary", segment))[0] == "summary_segment"
    assert decode(sse.segment_event("detail", segment))[0] == "detail_segment"


def test_late_web_sources_use_a_distinct_event_kind() -> None:
    source = SourceRef(id="W1", kind=SourceKind.web, title="WHO")

    assert decode(sse.source_event(source))[0] == "source"
    assert decode(sse.source_event(source, late=True))[0] == "late_source"


def test_done_event_carries_the_terminal_status() -> None:
    done = AnswerStreamDone(
        answer_id=HEADER.answer_id,
        status=AnswerStatus.insufficient_basis,
        provenance_ref="p",
    )
    event, data = decode(sse.done_event(done))

    assert event == "done"
    assert data["done"]["status"] == "insufficient_basis"


def test_error_event_carries_a_stable_code() -> None:
    _, data = decode(sse.error_event("pipeline_overloaded", "too many", retryable=True))

    assert data["error"]["code"] == "pipeline_overloaded"
    assert data["error"]["retryable"] is True


def test_heartbeat_is_an_sse_comment_invisible_to_clients() -> None:
    assert sse.HEARTBEAT_FRAME.startswith(b":")


def test_stream_headers_disable_buffering_and_caching() -> None:
    headers = sse.stream_headers(HEADER.answer_id)

    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Accel-Buffering"] == "no"
    # Lets a client that lost the stream resume without parsing the body.
    assert headers["X-Answer-Id"] == str(HEADER.answer_id)


def test_an_event_carrying_the_wrong_payload_does_not_construct() -> None:
    """The contract validates the union, so a framing bug is a
    ValidationError at the source rather than a confused client."""
    with pytest.raises(ValidationError):
        AnswerStreamEvent(event=StreamEventKind.done, header=HEADER)
    with pytest.raises(ValidationError):
        AnswerStreamEvent(event=StreamEventKind.header)


# ── insufficient basis ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reason",
    ["no_passages", "binder_failed", "synthesis_unavailable", "retrieval_unavailable"],
)
@pytest.mark.parametrize("locale", ["uk", "en"])
def test_every_reason_composes_in_every_locale(reason: str, locale: str) -> None:
    segments, flags = insufficient.compose(reason, locale=locale)

    assert [s.kind for s in segments] == [SegmentKind.missing_info, SegmentKind.next_step]
    assert all(s.text.strip() for s in segments)
    assert flags[0].code == "insufficient_basis"
    assert flags[0].message == reason


def test_the_fallback_never_produces_an_evidence_segment() -> None:
    """There is no evidence — a segment claiming otherwise is the exact
    failure this state exists to prevent."""
    for reason in ("no_passages", "binder_failed"):
        segments, _ = insufficient.compose(reason, locale="en")
        assert all(s.kind is not SegmentKind.evidence for s in segments)


def test_locales_actually_differ() -> None:
    uk, _ = insufficient.compose("no_passages", locale="uk")
    en, _ = insufficient.compose("no_passages", locale="en")
    assert uk[0].text != en[0].text


def test_unknown_reason_degrades_to_the_generic_message() -> None:
    segments, flags = insufficient.compose("something_new", locale="en")
    assert segments[0].text
    assert flags[0].message == "something_new"


# ── prompts ─────────────────────────────────────────────────────────────


def test_prompts_load_with_front_matter_and_a_fingerprint() -> None:
    for prompt in (prompts.triage_prompt(), prompts.intent_prompt(), prompts.synthesis_prompt()):
        assert prompt.version
        assert prompt.role.startswith("generator.")
        assert len(prompt.sha256) == 64
        assert prompt.pin.startswith(prompt.version + "+")


def test_rationale_comments_are_stripped_before_the_model_sees_the_prompt() -> None:
    """The HTML comment blocks explain the design to whoever edits the file;
    shipping them would waste context and leak reasoning into the prompt."""
    for prompt in (prompts.triage_prompt(), prompts.intent_prompt(), prompts.synthesis_prompt()):
        assert "<!--" not in prompt.text


def test_synthesis_prompt_has_the_placeholders_the_builder_fills() -> None:
    text = prompts.synthesis_prompt().text
    assert "{question}" in text
    assert "{evidence_blocks}" in text


def test_classifier_prompts_take_only_a_question() -> None:
    for prompt in (prompts.triage_prompt(), prompts.intent_prompt()):
        assert "{question}" in prompt.text
        assert "{evidence_blocks}" not in prompt.text


def test_all_versions_maps_names_to_pins_for_provenance() -> None:
    versions = prompts.all_versions()
    assert set(versions) == {"triage", "intent", "synthesis"}
    assert all("+" in pin for pin in versions.values())


def test_synthesis_temperature_ceiling_respects_lm2() -> None:
    assert prompts.synthesis_prompt().temperature_max <= 0.2


# ── concurrency cap ─────────────────────────────────────────────────────


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def decr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) - 1
        return self.values[key]

    async def expire(self, key: str, ttl: int) -> None:
        self.expires[key] = ttl

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


def slot(redis: FakeRedis, *, limit: int = 2) -> InFlightSlot:
    return InFlightSlot(
        redis,  # type: ignore[arg-type]
        tenant_id=uuid4(),
        user_sub=uuid4(),
        limit=limit,
        ttl_seconds=180,
    )


async def test_slots_up_to_the_limit_are_granted() -> None:
    redis = FakeRedis()
    tenant, user = uuid4(), uuid4()

    def make() -> InFlightSlot:
        return InFlightSlot(redis, tenant_id=tenant, user_sub=user, limit=2, ttl_seconds=180)  # type: ignore[arg-type]

    async with make(), make():
        pass  # both acquired


async def test_exceeding_the_limit_raises_pipeline_overloaded() -> None:
    redis = FakeRedis()
    tenant, user = uuid4(), uuid4()

    def make() -> InFlightSlot:
        return InFlightSlot(redis, tenant_id=tenant, user_sub=user, limit=2, ttl_seconds=180)  # type: ignore[arg-type]

    async with make(), make():
        with pytest.raises(PipelineOverloadedError):
            async with make():
                pass


async def test_a_refused_slot_does_not_consume_capacity() -> None:
    """The rejection path must decrement what it incremented, or the third
    request permanently poisons the counter."""
    redis = FakeRedis()
    tenant, user = uuid4(), uuid4()

    def make() -> InFlightSlot:
        return InFlightSlot(redis, tenant_id=tenant, user_sub=user, limit=1, ttl_seconds=180)  # type: ignore[arg-type]

    async with make():
        with pytest.raises(PipelineOverloadedError):
            async with make():
                pass
    # Slot released; the next request is accepted.
    async with make():
        pass


async def test_slot_is_released_even_when_the_body_raises() -> None:
    redis = FakeRedis()
    holder = slot(redis, limit=1)

    with pytest.raises(RuntimeError):
        async with holder:
            raise RuntimeError("pipeline blew up")

    assert all(count <= 0 for count in redis.values.values())


async def test_ttl_is_refreshed_on_every_acquire() -> None:
    """A long-running second answer must not inherit the first one's
    remaining TTL, or its slot expires while it is still working."""
    redis = FakeRedis()
    tenant, user = uuid4(), uuid4()

    def make() -> InFlightSlot:
        return InFlightSlot(redis, tenant_id=tenant, user_sub=user, limit=2, ttl_seconds=180)  # type: ignore[arg-type]

    async with make():
        redis.expires.clear()
        async with make():
            assert redis.expires  # refreshed by the second acquire
