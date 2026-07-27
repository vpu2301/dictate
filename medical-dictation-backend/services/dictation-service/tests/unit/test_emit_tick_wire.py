"""``_emit_tick`` must actually serialize a windower tick onto the wire.

Regression for the defect found by running conversation mode end-to-end
against the deployed stack (sprint 14): the handler passed the
windower's ``asr_models.WordTiming`` objects straight into the protocol
models' ``words`` field, which is typed ``list[TokenTiming]``. The two
are field-identical but are DIFFERENT classes, and pydantic v2 does not
coerce one BaseModel instance into another — so constructing the
``Partial``/``PartialV2`` raised ``ValidationError``.

That exception escaped ``_emit_tick`` into ``_window_loop``, which ran
as a bare ``create_task`` with no error path: the window loop died on
the very FIRST partial of every session, in both protocol versions.
The session kept accepting audio, kept looking healthy, stored its
audio — and finalized an empty transcript. Nothing was logged.

So these tests assert two things that together make that failure
impossible to reintroduce silently:
  1. a tick carrying real ``WordTiming``s serializes for v1 AND v2;
  2. a tick that raises does not kill the loop, and repeated failures
     fail the session loudly instead of transcribing nothing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest

from asr_models import Segment, WordTiming
from dictation_service.inference.windower import TickOutput
from dictation_service.protocol import PROTOCOL_VERSION_V1, PROTOCOL_VERSION_V2
from dictation_service.session.state import SessionState
from dictation_service.ws import handler as ws_handler


class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


class _FakeBuffer:
    total_ms = 8_000


def _words() -> list[WordTiming]:
    return [
        WordTiming(text="Доброго", start_ms=100, end_ms=600, probability=0.91),
        WordTiming(text="дня", start_ms=620, end_ms=980, probability=0.88),
    ]


def _segment(words: list[WordTiming]) -> Segment:
    return Segment(
        text=" ".join(w.text for w in words),
        start_ms=words[0].start_ms,
        end_ms=words[-1].end_ms,
        words=words,
        avg_confidence=0.9,
    )


def _ctx(protocol_version: int, mode: str) -> Any:
    ctx = ws_handler.SessionContext(
        session_id=uuid4(),
        tenant_id=uuid4(),
        user_id="user-1",
        language="uk",
        prompt_id=uuid4(),
        prompt_text="",
        target_kind="generic",
        encounter_id=uuid4(),
        template_id=None,
        mode=mode,
        protocol_version=protocol_version,
    )
    ctx.ws = _FakeWs()
    ctx.buffer = _FakeBuffer()
    ctx.state = SessionState.ACTIVE
    return ctx


@pytest.mark.parametrize(
    ("protocol_version", "mode"),
    [(PROTOCOL_VERSION_V1, "dictation"), (PROTOCOL_VERSION_V2, "conversation")],
)
def test_emit_tick_serializes_wordtimings(protocol_version: int, mode: str) -> None:
    """The windower's own word objects must reach the wire, both versions."""
    ctx = _ctx(protocol_version, mode)
    words = _words()
    tick = TickOutput(
        new_partial=_segment(words),
        new_finals=[_segment(words)],
        window_start_ms=0,
        window_end_ms=4_000,
    )

    asyncio.run(ws_handler._emit_tick(ctx, tick))

    kinds = [m["type"] for m in ctx.ws.sent]
    assert "partial" in kinds, f"no partial reached the wire: {kinds}"
    assert "final" in kinds, f"no final reached the wire: {kinds}"
    for message in ctx.ws.sent:
        if message["type"] in {"partial", "final"}:
            assert [w["text"] for w in message["words"]] == ["Доброго", "дня"]
            assert message["text"] == "Доброго дня"


def test_window_loop_survives_a_failing_tick() -> None:
    """One bad tick is logged and skipped — the transcript continues."""
    ctx = _ctx(PROTOCOL_VERSION_V2, "conversation")
    stop = asyncio.Event()
    calls = 0

    async def flaky_tick(*_args: Any, **_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("boom")
        if calls >= 3:
            stop.set()

    async def run() -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ws_handler, "_window_tick", flaky_tick)
            mp.setattr(ws_handler.settings, "window_tick_interval_ms", 1)
            await asyncio.wait_for(
                ws_handler._window_loop(ctx, object(), object(), stop), timeout=5
            )

    asyncio.run(run())
    assert calls >= 3, "the loop stopped ticking after one failure"
    assert ctx.state == SessionState.ACTIVE


def test_window_loop_fails_the_session_when_every_tick_fails() -> None:
    """A permanently broken transcriber must not masquerade as recording."""
    ctx = _ctx(PROTOCOL_VERSION_V2, "conversation")
    stop = asyncio.Event()
    failed: dict[str, Any] = {}

    async def always_fails(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("boom")

    async def fake_on_failed(_ctx: Any, _state: Any, *, kind: str, detail: str) -> None:
        failed["kind"] = kind
        failed["detail"] = detail
        _ctx.state = SessionState.FAILED

    async def run() -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ws_handler, "_window_tick", always_fails)
            mp.setattr(ws_handler, "_on_failed", fake_on_failed)
            mp.setattr(ws_handler.settings, "window_tick_interval_ms", 1)
            await asyncio.wait_for(
                ws_handler._window_loop(ctx, object(), object(), stop), timeout=5
            )

    asyncio.run(run())
    assert failed["kind"] == "worker_failed"
    assert ctx.state == SessionState.FAILED


def test_transcript_jsonb_decodes_from_a_json_string() -> None:
    """``GET /dictate/sessions/{id}`` must survive asyncpg's jsonb shape.

    No jsonb codec is registered on the pool (finalize writes the column
    with ``json.dumps`` for exactly that reason), so the column reads
    back as a JSON *string*. Passing it straight to the response model
    raised ``ValidationError`` → 500 on every read of the endpoint. The
    conversation review swallows that error and falls back to what it
    rendered live, which is why it stayed hidden until there was a
    transcript worth reading back.
    """
    from dictation_service.routers.sessions import _transcript_from_row

    segments = [{"text": "Доброго дня", "start_ms": 0, "end_ms": 980, "speaker": "S1"}]

    assert _transcript_from_row(json.dumps(segments)) == segments
    # An empty session reads back as the string '[]', not a list.
    assert _transcript_from_row("[]") == []
    assert _transcript_from_row(None) == []
    # A pool that DOES decode jsonb (or a test double) still works.
    assert _transcript_from_row(segments) == segments
