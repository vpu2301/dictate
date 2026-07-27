"""Sprint-14 speaker-mapping wire contract (handler helpers, pure).

The inference itself is proven in ``test_diarization_mapping.py``; this
pins how it reaches the wire:

* ``_current_mapping_hint`` — what rides along on every v2 partial/final.
* ``_maybe_emit_mapping_update`` — emits + audits only on a NEW
  hypothesis, and never once the clinician has overridden.
* the ``SetSpeakerMapping`` branch of ``_on_text`` — the manual override
  is authoritative from the moment received: inference frozen, audited,
  acknowledged with ``manual=true, confidence=1.0``, and no further
  inferred update can ever follow.

No DB, no WebSocket, no models.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from dictation_service import audit_kinds
from dictation_service.diarization.mapping import (
    MappingHypothesis,
    SpeakerMappingInference,
)
from dictation_service.session.manager import SessionContext
from dictation_service.ws.handler import (
    _current_mapping_hint,
    _maybe_emit_mapping_update,
    _on_text,
)

DOCTOR_WORDS = ["призначу", "направлення", "рекомендую", "обстеження", "аналізи", "діагноз"]
PATIENT_WORDS = ["болить", "голова", "вже", "тиждень", "дуже", "сильно"]


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(t) for t in self.sent]


class _FakeAuditWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def write_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]


class _StubInference:
    """Drives ``evaluate()``/``current`` without any acoustic evidence."""

    def __init__(
        self,
        *,
        evaluates_to: MappingHypothesis | None = None,
        current: MappingHypothesis | None = None,
    ) -> None:
        self._evaluates_to = evaluates_to
        self.current = current
        self.evaluate_calls = 0

    def evaluate(self) -> MappingHypothesis | None:
        self.evaluate_calls += 1
        return self._evaluates_to


def _hypothesis() -> MappingHypothesis:
    return MappingHypothesis(
        mapping={"S1": "doctor", "S2": "patient"},
        confidence=0.82,
        rationale="opener 0.80 vs 0.20; clinician-register density 0.91 vs 0.09",
    )


def _ctx(
    *,
    mode: str = "conversation",
    inference: Any | None = None,
    manual: bool = False,
    ws: _FakeWebSocket | None = None,
) -> SessionContext:
    ctx = SessionContext(
        session_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        language="uk",
        prompt_id=uuid4(),
        prompt_text="",
        target_kind="generic",
        encounter_id=None,
        template_id=None,
        mode=mode,
        protocol_version=2,
        mapping_inference=inference,
        mapping_manual=manual,
    )
    ctx.ws = ws
    return ctx


def _state() -> SimpleNamespace:
    return SimpleNamespace(audit_writer=_FakeAuditWriter())


def _loaded_inference() -> SpeakerMappingInference:
    """A real inference carrying enough evidence to emit S1=doctor."""
    inference = SpeakerMappingInference(language="uk")
    for word in DOCTOR_WORDS:
        inference.observe_word(word, "S1")
    for word in PATIENT_WORDS:
        inference.observe_word(word, "S2")
    return inference


# ── _current_mapping_hint ────────────────────────────────────────────


def test_hint_is_none_without_an_inference() -> None:
    assert _current_mapping_hint(_ctx(inference=None)) is None


def test_hint_is_none_before_the_first_hypothesis() -> None:
    ctx = _ctx(inference=_StubInference(current=None))
    assert _current_mapping_hint(ctx) is None


def test_hint_is_a_copy_of_the_current_mapping() -> None:
    hypothesis = _hypothesis()
    ctx = _ctx(inference=_StubInference(current=hypothesis))

    hint = _current_mapping_hint(ctx)

    assert hint == {"S1": "doctor", "S2": "patient"}
    hint["S1"] = "patient"  # mutating the hint must not corrupt the belief
    assert hypothesis.mapping["S1"] == "doctor"


# ── _maybe_emit_mapping_update ───────────────────────────────────────


async def test_new_hypothesis_is_emitted_and_audited() -> None:
    ws = _FakeWebSocket()
    ctx = _ctx(inference=_StubInference(evaluates_to=_hypothesis()), ws=ws)
    state = _state()

    await _maybe_emit_mapping_update(ctx, state)

    frames = ws.frames()
    assert len(frames) == 1
    frame = frames[0]
    assert frame["type"] == "speaker_mapping_updated"
    assert frame["session_id"] == str(ctx.session_id)
    assert frame["mapping"] == {"S1": "doctor", "S2": "patient"}
    assert frame["confidence"] == 0.82
    assert frame["manual"] is False
    assert "clinician-register density" in frame["rationale"]

    assert state.audit_writer.kinds() == [audit_kinds.SPEAKER_MAPPING_INFERRED]
    payload = state.audit_writer.events[0]["payload"]
    assert payload["mapping"] == {"S1": "doctor", "S2": "patient"}
    assert payload["confidence"] == 0.82
    assert ctx.speaker_mapping_updates == 1


async def test_unchanged_hypothesis_emits_nothing() -> None:
    ws = _FakeWebSocket()
    ctx = _ctx(inference=_StubInference(evaluates_to=None), ws=ws)
    state = _state()

    await _maybe_emit_mapping_update(ctx, state)

    assert ws.sent == []
    assert state.audit_writer.events == []
    assert ctx.speaker_mapping_updates == 0


async def test_manual_override_suppresses_inferred_updates() -> None:
    ws = _FakeWebSocket()
    inference = _StubInference(evaluates_to=_hypothesis())
    ctx = _ctx(inference=inference, manual=True, ws=ws)
    state = _state()

    await _maybe_emit_mapping_update(ctx, state)

    assert ws.sent == []
    assert state.audit_writer.events == []
    # Not even consulted — the clinician's word is final.
    assert inference.evaluate_calls == 0


async def test_no_inference_or_no_socket_emits_nothing() -> None:
    state = _state()
    await _maybe_emit_mapping_update(_ctx(inference=None, ws=_FakeWebSocket()), state)
    await _maybe_emit_mapping_update(
        _ctx(inference=_StubInference(evaluates_to=_hypothesis()), ws=None), state
    )
    assert state.audit_writer.events == []


# ── SetSpeakerMapping over _on_text ──────────────────────────────────


def _set_mapping_frame(mapping: dict[str, str]) -> str:
    return json.dumps({"type": "set_speaker_mapping", "mapping": mapping})


async def test_manual_set_freezes_the_inference_and_acknowledges() -> None:
    ws = _FakeWebSocket()
    inference = _loaded_inference()
    ctx = _ctx(inference=inference, ws=ws)
    state = _state()

    cont = await _on_text(
        ctx, ws, state, _set_mapping_frame({"S1": "patient", "S2": "doctor"}), None
    )

    assert cont is True  # the session stays open
    assert inference.frozen is True
    assert inference.frozen_mapping == {"S1": "patient", "S2": "doctor"}
    assert ctx.mapping_manual is True

    assert state.audit_writer.kinds() == [audit_kinds.SPEAKER_MAPPING_MANUAL_SET]
    event = state.audit_writer.events[0]
    assert event["kind"] == "conversation.speaker_mapping.manual_set"
    assert event["payload"]["mapping"] == {"S1": "patient", "S2": "doctor"}
    assert event["target_id"] == str(ctx.session_id)

    frames = ws.frames()
    assert len(frames) == 1
    assert frames[0]["type"] == "speaker_mapping_updated"
    assert frames[0]["mapping"] == {"S1": "patient", "S2": "doctor"}
    assert frames[0]["manual"] is True
    assert frames[0]["confidence"] == 1.0

    # The manual mapping is what rides on subsequent v2 frames.
    assert _current_mapping_hint(ctx) == {"S1": "patient", "S2": "doctor"}


async def test_inference_never_speaks_again_after_a_manual_set() -> None:
    ws = _FakeWebSocket()
    inference = _loaded_inference()

    # Baseline: this evidence WOULD produce an update.
    probe_ctx = _ctx(inference=_loaded_inference(), ws=_FakeWebSocket())
    await _maybe_emit_mapping_update(probe_ctx, _state())
    assert probe_ctx.ws.frames()[0]["type"] == "speaker_mapping_updated"

    ctx = _ctx(inference=inference, ws=ws)
    state = _state()
    await _on_text(ctx, ws, state, _set_mapping_frame({"S1": "doctor", "S2": "patient"}), None)
    ws.sent.clear()

    await _maybe_emit_mapping_update(ctx, state)

    assert ws.sent == []
    assert inference.evaluate() is None  # frozen at the source too
    assert state.audit_writer.kinds() == [audit_kinds.SPEAKER_MAPPING_MANUAL_SET]


async def test_set_speaker_mapping_on_a_dictation_session_is_rejected() -> None:
    ws = _FakeWebSocket()
    # A v2 dictation session: the message can arrive on the wire, but the
    # session has no mapping to set.
    ctx = _ctx(mode="dictation", inference=None, ws=ws)
    state = _state()

    cont = await _on_text(ctx, ws, state, _set_mapping_frame({"S1": "doctor"}), None)

    assert cont is True
    frames = ws.frames()
    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert frames[0]["code"] == "bad_message"
    assert frames[0]["recoverable"] is True
    assert ctx.mapping_manual is False
    assert state.audit_writer.events == []
