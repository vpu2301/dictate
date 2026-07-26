"""Sprint-14 conversation start gating in ``_new_session`` (pure).

Conversation mode records the PATIENT, so a start is refused before a
single audio frame unless there is an encounter AND a granted
``recording`` consent for its patient. Dictation mode must be entirely
unaffected — it never even looks a consent up.

Everything below the handler is faked: ``tenant_connection`` is replaced
with an async CM over a dummy connection and the three domain readers
(``repository.count_active_for_tenant``, ``encounters.fetch_encounter_status``,
``consents.fetch_recording_consent``) plus ``repository.insert_session``
are monkeypatched on their own modules — the handler resolves them as
module attributes at call time, so the real ``encounter_gate`` /
``consent_gate`` decision logic still runs. The tmpfs ring buffer and the
Opus decoder are stubbed so no OS resources are touched.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from auth import Claims
from dictation_service import audit_kinds
from dictation_service.diarization.engine import DiarizationUnavailableError
from dictation_service.domain import consents as consents_mod
from dictation_service.domain import encounters as encounters_mod
from dictation_service.domain import repository as repository_mod
from dictation_service.protocol.messages import StartSession, StartSessionV2
from dictation_service.session.manager import SessionManager
from dictation_service.ws import handler as handler_mod
from dictation_service.ws.handler import _new_session
from dictation_service.ws.upgrade import UpgradeContext

TENANT_ID = uuid4()
USER_ID = uuid4()
PROMPT_ID = uuid4()


# ── Fakes ────────────────────────────────────────────────────────────


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed_with: list[int] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed_with.append(code)

    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(t) for t in self.sent]


class _FakeAuditWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def write_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]

    def by_kind(self, kind: str) -> dict[str, Any]:
        matches = [e for e in self.events if e["kind"] == kind]
        assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
        return matches[0]


class _FakeDiarizationStream:
    segments: list[Any] = []

    def attribute(self, start_ms: int, end_ms: int) -> tuple[str | None, float | None]:
        return None, None


class _FakeDiarizationEngine:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises
        self.ensure_loaded_calls = 0
        self.new_stream_calls = 0

    async def ensure_loaded(self) -> None:
        self.ensure_loaded_calls += 1
        if self.raises is not None:
            raise self.raises

    def new_stream(self) -> _FakeDiarizationStream:
        self.new_stream_calls += 1
        return _FakeDiarizationStream()


class _FakeBuffer:
    total_ms = 0

    def __init__(self, *, session_id: UUID) -> None:
        self.session_id = session_id
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Calls:
    """Counters for the monkeypatched domain readers."""

    def __init__(self) -> None:
        self.count_active = 0
        self.encounter_status = 0
        self.consent_fetch = 0
        self.inserted: list[dict[str, Any]] = []


def _claims() -> Claims:
    now = int(time.time())
    return Claims(
        sub=USER_ID,
        tid=TENANT_ID,
        roles=["clinician"],
        sid="sess-1",
        iss="https://kc.example/realms/mdx",
        aud="medical-dictation",
        exp=now + 3600,
        iat=now,
    )


def _upgrade(*, protocol_version: int) -> UpgradeContext:
    return UpgradeContext(
        claims=_claims(),
        subprotocol=("medical-dictation.v2" if protocol_version == 2 else "medical-dictation.v1"),
        client_ip="127.0.0.1",
        origin=None,
        protocol_version=protocol_version,
        bearer="tok",
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    encounter_status: str | None = "in_progress",
    consent: dict[str, Any] | None = None,
    active_sessions: int = 0,
    engine_raises: Exception | None = None,
) -> tuple[SimpleNamespace, _FakeWebSocket, _Calls]:
    calls = _Calls()

    @asynccontextmanager
    async def _fake_tenant_connection(_pool: Any, _tenant_id: UUID) -> Any:
        yield SimpleNamespace()

    async def _count_active(_conn: Any, *, tenant_id: UUID) -> int:
        calls.count_active += 1
        return active_sessions

    async def _fetch_encounter_status(_conn: Any, *, encounter_id: UUID) -> str | None:
        calls.encounter_status += 1
        return encounter_status

    async def _fetch_recording_consent(_conn: Any, *, encounter_id: UUID) -> dict[str, Any] | None:
        calls.consent_fetch += 1
        return consent

    async def _insert_session(_conn: Any, **kwargs: Any) -> None:
        calls.inserted.append(kwargs)

    async def _fetch_prompt_text(_state: Any, _tid: UUID, _pid: UUID) -> str | None:
        return "медичний контекст"

    monkeypatch.setattr(handler_mod, "tenant_connection", _fake_tenant_connection)
    monkeypatch.setattr(repository_mod, "count_active_for_tenant", _count_active)
    monkeypatch.setattr(repository_mod, "insert_session", _insert_session)
    monkeypatch.setattr(encounters_mod, "fetch_encounter_status", _fetch_encounter_status)
    monkeypatch.setattr(consents_mod, "fetch_recording_consent", _fetch_recording_consent)
    monkeypatch.setattr(handler_mod, "_fetch_prompt_text", _fetch_prompt_text)
    monkeypatch.setattr(handler_mod, "SessionAudioBuffer", _FakeBuffer)
    monkeypatch.setattr(handler_mod, "OpusDecoder", lambda: SimpleNamespace())

    state = SimpleNamespace(
        app_pool=SimpleNamespace(),
        session_manager=SessionManager(max_sessions=4),
        audit_writer=_FakeAuditWriter(),
        diarization_engine=_FakeDiarizationEngine(raises=engine_raises),
        engine=SimpleNamespace(model_name="large-v3"),
        template_client=None,
    )
    return state, _FakeWebSocket(), calls


def _start_v2(*, encounter_id: UUID | None, mode: str = "conversation") -> StartSessionV2:
    return StartSessionV2(
        prompt_id=PROMPT_ID,
        language="uk",
        encounter_id=encounter_id,
        mode=mode,
    )


# ── a. conversation without an encounter ─────────────────────────────


async def test_conversation_without_encounter_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, ws, calls = _wire(monkeypatch)

    ctx = await _new_session(ws, _upgrade(protocol_version=2), state, _start_v2(encounter_id=None))

    assert ctx is None
    frames = ws.frames()
    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert frames[0]["code"] == "consent_required"
    assert frames[0]["recoverable"] is False
    assert ws.closed_with == [1008]

    event = state.audit_writer.by_kind(audit_kinds.CONSENT_REFUSED)
    assert event["kind"] == "conversation.consent_refused"
    assert event["severity"].value == "warn"
    assert event["tenant_id"] == TENANT_ID
    assert event["actor_sub"] == USER_ID
    assert event["target_id"] == "none"

    # Refused before touching the DB at all.
    assert calls.encounter_status == 0
    assert calls.consent_fetch == 0
    assert state.session_manager.total_weight == 0


# ── b. conversation with an encounter but no consent ─────────────────


async def test_conversation_without_granted_consent_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, ws, calls = _wire(monkeypatch, consent=None)
    encounter_id = uuid4()

    ctx = await _new_session(
        ws, _upgrade(protocol_version=2), state, _start_v2(encounter_id=encounter_id)
    )

    assert ctx is None
    assert calls.consent_fetch == 1
    frames = ws.frames()
    assert frames[-1]["type"] == "error"
    assert frames[-1]["code"] == "consent_required"
    assert ws.closed_with == [1008]

    event = state.audit_writer.by_kind(audit_kinds.CONSENT_REFUSED)
    assert event["severity"].value == "warn"
    assert event["target_kind"] == "encounter"
    assert event["target_id"] == str(encounter_id)
    assert state.session_manager.total_weight == 0


# ── c. conversation with encounter + consent proceeds ────────────────


async def test_conversation_with_consent_starts_a_weighted_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patient_id = uuid4()
    state, ws, calls = _wire(monkeypatch, consent={"patient_id": patient_id, "consent_id": uuid4()})
    encounter_id = uuid4()

    ctx = await _new_session(
        ws, _upgrade(protocol_version=2), state, _start_v2(encounter_id=encounter_id)
    )

    assert ctx is not None
    assert ctx.mode == "conversation"
    assert ctx.patient_id == patient_id
    assert ctx.protocol_version == 2
    assert ctx.bearer == "tok"
    assert ctx.capacity_weight == 2
    assert ctx.diarization is not None
    assert ctx.mapping_inference is not None
    assert ctx.mapping_manual is False

    # Registered, and it costs two slots.
    assert state.session_manager.get(ctx.session_id) is ctx
    assert state.session_manager.total_weight == 2
    assert state.session_manager.total_count == 1

    # Diarizer was proven loadable before any audio was accepted.
    assert state.diarization_engine.ensure_loaded_calls == 1
    assert state.diarization_engine.new_stream_calls == 1

    assert calls.inserted and calls.inserted[0]["mode"] == "conversation"

    started = ws.frames()[-1]
    assert started["type"] == "session_started"
    assert started["protocol_version"] == 2
    assert started["mode"] == "conversation"
    assert started["session_id"] == str(ctx.session_id)
    assert started["model"] == "large-v3"
    assert ws.closed_with == []

    audit = state.audit_writer.by_kind(audit_kinds.SESSION_STARTED)
    assert audit["payload"]["mode"] == "conversation"
    assert audit["payload"]["protocol_version"] == 2
    assert audit_kinds.CONSENT_REFUSED not in state.audit_writer.kinds()


# ── d. dictation is untouched ────────────────────────────────────────


async def test_v1_dictation_start_never_looks_up_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, ws, calls = _wire(monkeypatch)

    ctx = await _new_session(
        ws,
        _upgrade(protocol_version=1),
        state,
        StartSession(prompt_id=PROMPT_ID, language="uk"),
    )

    assert ctx is not None
    assert ctx.mode == "dictation"
    assert ctx.capacity_weight == 1
    assert ctx.diarization is None
    assert ctx.mapping_inference is None
    assert ctx.patient_id is None
    assert state.session_manager.total_weight == 1

    # The consent read is conversation-only — not a widened gate.
    assert calls.consent_fetch == 0
    assert calls.encounter_status == 0
    assert state.diarization_engine.ensure_loaded_calls == 0

    started = ws.frames()[-1]
    assert started["type"] == "session_started"
    assert started["protocol_version"] == 1
    assert "mode" not in started  # v1 wire stays byte-stable


async def test_v2_dictation_mode_is_weight_one_and_consent_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, ws, calls = _wire(monkeypatch)

    ctx = await _new_session(
        ws,
        _upgrade(protocol_version=2),
        state,
        _start_v2(encounter_id=None, mode="dictation"),
    )

    assert ctx is not None
    assert ctx.mode == "dictation"
    assert ctx.capacity_weight == 1
    assert calls.consent_fetch == 0
    assert ws.frames()[-1]["mode"] == "dictation"


# ── e. diarizer unavailable ──────────────────────────────────────────


async def test_unloadable_diarizer_fails_loud_not_as_a_consent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, ws, _calls = _wire(
        monkeypatch,
        consent={"patient_id": uuid4(), "consent_id": uuid4()},
        engine_raises=DiarizationUnavailableError("ecapa weights missing"),
    )

    ctx = await _new_session(
        ws, _upgrade(protocol_version=2), state, _start_v2(encounter_id=uuid4())
    )

    assert ctx is None
    frame = ws.frames()[-1]
    assert frame["type"] == "error"
    assert frame["code"] == "worker_failed"
    assert frame["recoverable"] is True
    assert "ecapa weights missing" in frame["detail"]
    assert ws.closed_with == [1013]

    # A loadable-diarizer failure must never be reported as a consent
    # problem — the clinician would chase the wrong fix.
    assert audit_kinds.CONSENT_REFUSED not in state.audit_writer.kinds()
    assert state.session_manager.total_weight == 0
